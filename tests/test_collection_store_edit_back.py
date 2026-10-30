"""Edit-back of store collection views (OpenSpec move-structured-collections-to-sqlite §6, §16 A1, A2).

The writer-level cases drive ``edit_back.EditBack`` with explicit monotonic times;
the service cases drive the installed service, with and without watcher notifications.
"""

import hashlib
import json
import os
import sqlite3
import threading
import time

import pytest
from test_collection_store_migration import env as env
from test_collection_store_service import service as service
from test_collection_store_writer import CID, KEY, create, manifest_path, manifest_text
from test_collection_store_writer import store as store

from exomem import get_page
from exomem import structured_collections as collections
from exomem.collection_store import capability, edit_back

SETTLED = edit_back.SETTLE_SECONDS
HELD = edit_back.INVALID_SECONDS
VIEWS = capability.COLLECTION_VIEWS_V1


@pytest.fixture(autouse=True)
def views_released(monkeypatch):
    """Each case runs under a release that enables edit-back, unless it sets its own release."""
    monkeypatch.setattr(capability, "RELEASED", capability.RELEASED | {VIEWS})


def passes(store, *times):
    """Drain the bounded turns due at each supplied monotonic time."""
    runner = edit_back.EditBack()
    runner.hints(store, (), True)
    for now in times:
        until(lambda now=now: runner.run(store, now=now) > 0)
        store.reconcile_views(pending_only=True, missing_ready=runner._state(store).missing_ready)
    return runner


def txns(store, operation=None):
    query = "SELECT operation,actor,request_id FROM txns" + (" WHERE operation=?" if operation else "")
    return store.connection.execute(query, (operation,) if operation else ()).fetchall()


def holds(store):
    return store.connection.execute(
        "SELECT held_id,code,held_bytes FROM held_candidates WHERE kind='view-correction' ORDER BY held_id"
    ).fetchall()


def appended(store, **values):
    create(store)
    receipt = store.append_record(CID, item={"title": "One", **values}, item_key=KEY, why="observe")
    store.reconcile_views()
    return receipt, store.root / receipt["affected_paths"][0]


def test_a_settled_valid_edit_is_one_view_edit_and_a_replay_commits_nothing(store):
    """Defect: a valid edit of the current view is deferred forever, or is applied twice."""
    receipt, path = appended(store, count=1)
    edited = path.read_bytes().replace(b"title: One", b"title: Edited").replace(b"count: 1", b"count: 2")
    path.write_bytes(edited)
    runner = passes(store, 0.0, SETTLED)
    assert txns(store, "view_edit") == [("view_edit", "owner:view-edit", edit_back.request_id(
        receipt["affected_paths"][0], 1, hashlib.sha256(edited).hexdigest()))]
    row = store.connection.execute("SELECT row_version,values_json FROM items").fetchone()
    assert row == (2, json.dumps({"count": 2, "title": "Edited"}, separators=(",", ":")))
    assert get_page.get_page(store.root, path=receipt["affected_paths"][0]).frontmatter["exomem_view"]["v"] == 2
    assert holds(store) == [] and not list(path.parent.glob(".exomem-collection-aside-*"))
    # The same bytes again, as after a crash before their render was installed: nothing new.
    rendered = path.read_bytes()
    path.write_bytes(edited)
    until(lambda: runner.run(store, now=SETTLED + 60) > 0)
    until(lambda: runner.run(store, now=2 * SETTLED + 60) > 0)
    assert len(txns(store, "view_edit")) == 1 and holds(store) == [] and path.read_bytes() == rendered


def test_an_item_save_without_its_stamp_is_held_without_changing_the_record(store):
    """Removing generated metadata must not authorize an edit against an unknown base."""
    from exomem import vault

    _, path = appended(store, count=1)
    frontmatter, body, _ = vault.parse_frontmatter(path.read_text(), strict=True)
    frontmatter.pop("exomem_view")
    frontmatter["title"] = "Edited without a base"
    edited = ("---\n" + vault.serialize_frontmatter(frontmatter) + "\n---\n" + body).encode()
    path.write_bytes(edited)

    passes(store, 0.0, SETTLED, HELD + SETTLED)

    [(_, code, raw)] = holds(store)
    assert (code, raw) == ("VIEW_INVALID", edited)
    assert store.connection.execute("SELECT row_version,values_json FROM items").fetchone() == (
        1, '{"count":1,"title":"One"}')
    assert txns(store, "view_edit") == []


def test_a_drain_without_a_census_leaves_a_renamed_then_updated_view_pending(store):
    """Defect: an installed view that the owner renamed and an agent then updated looks never installed
    to a drain, which runs no census, so it recreates the old path the owner moved away from."""
    receipt, old = appended(store)
    old.rename(old.parent / "Manual.md")
    store.update_record(CID, item_key=KEY, changes={"count": 2}, why="agent",
                        expected_container_hash=store.current_guards(CID)[0],
                        expected_item_version=receipt["after_item_hash"])
    for _ in range(2):  # what the stop drain passes: one install pass and one confirming pass
        store.reconcile_views(pending_only=True, missing_ready=lambda cid: False)
    assert not old.exists()
    assert store.connection.execute("SELECT state FROM projection_state WHERE kind='item'").fetchone() == ("pending",)


def test_an_offline_cross_directory_move_keeps_the_filename_and_payload_version(store):
    """An offline rename must be found before the missing old view is recreated."""
    receipt, old = appended(store)
    raw = old.read_bytes()
    destination = old.parent / "Renamed" / "Manual.md"
    destination.parent.mkdir()
    old.rename(destination)
    passes(store, 0.0, SETTLED)
    path, version, payload = store.connection.execute("SELECT view_path,row_version,payload_hash FROM items").fetchone()
    assert path == destination.relative_to(store.root).as_posix() and version == 1
    assert receipt["after_item_hash"] == store._version({"collection_id": CID, "item_key": KEY,
                                                       "row_version": version, "payload_hash": payload})
    assert destination.read_bytes() == raw and not old.exists() and holds(store) == []
    assert [(operation, actor) for operation, actor, _ in txns(store, "view_move")] == [("view_move", "owner:view-edit")]


def test_a_move_event_remains_verifiable_after_its_formatting_correction_is_discarded(store):
    """Discarded file formatting must not remove an input needed to verify immutable move history."""
    from exomem import records

    _, source = appended(store)
    destination = source.with_name("Manual.md")
    source.rename(destination)
    edited = destination.read_bytes().replace(b"title: One", b"title: 'One'")
    destination.write_bytes(edited)
    passes(store, 0.0, SETTLED)
    assert store.connection.execute("SELECT view_path FROM items").fetchone() == (
        destination.relative_to(store.root).as_posix(),)
    assert any(raw == edited for _, _, raw in holds(store))
    for reference, _, _ in holds(store):
        store.discard_held(CID, held=reference, why="discard formatting correction")
    assert holds(store) == []

    cursor = store.connection.execute(
        "SELECT t.*,m.manifest_text,c.manifest_path FROM txns t "
        "JOIN collection_manifests m ON m.collection_id=t.collection_id "
        "AND m.manifest_version=t.manifest_version_after "
        "JOIN collections c ON c.collection_id=t.collection_id WHERE t.operation='view_move'")
    [row] = cursor.fetchall()
    txn = dict(zip((column[0] for column in cursor.description), row, strict=True))
    receipt = json.loads(txn["receipt_json"])
    version = store.connection.execute(
        "SELECT version_before FROM audit_effects WHERE txn_id=?", (txn["txn_id"],)).fetchone()[0]
    manifest = collections.parse_manifest_bytes(
        store.root, txn["manifest_path"], txn["manifest_text"].encode())
    # The v1 event envelope uses only immutable transaction, manifest, receipt and effect facts.
    event = {name: txn[name] for name in (
        "transition_id", "collection_id", "operation", "generation_before", "generation_after",
        "manifest_version_before", "manifest_version_after", "prev_event_hash", "committed_at", "why", "request_hash")}
    event.update(manifest_hash=manifest.manifest_version.hash,
                 effects={"old_path": receipt["affected_paths"][0], "path": receipt["affected_paths"][1],
                          "generation": txn["generation_before"], "version": version})
    assert hashlib.sha256(b"exomem-collection-event:v1\0" + records._canonical_json(event)).hexdigest() == txn["event_hash"]


def test_a_file_move_hint_completes_the_census_when_each_turn_visits_one_path(store):
    """An exact file hint must finish at window size one instead of preventing move discovery forever."""
    _, source = appended(store)
    canonical = source.read_bytes()
    destination = source.with_name("Manual.md")
    source.rename(destination)
    runner = edit_back.EditBack()
    runner.hints(store, [(source.relative_to(store.root).as_posix(), destination.relative_to(store.root).as_posix(), False)])
    for now in (0.0, SETTLED):
        for _ in range(200):
            if runner.run(store, now=now, limit=1) > 0:
                break
        else:
            pytest.fail("the file-move census did not finish its bounded turns")
    assert runner.last_status["status"] == "current"
    assert store.connection.execute("SELECT view_path,row_version FROM items").fetchone() == (
        destination.relative_to(store.root).as_posix(), 1)
    assert len(txns(store, "view_move")) == 1 and holds(store) == []
    assert destination.read_bytes() == canonical and not source.exists()


def test_cyclic_moves_at_one_payload_version_do_not_replay_the_first_move(store):
    """A→B→A→B at the same item version is three moves, each with a fresh container generation."""
    _, first = appended(store)
    second = first.with_name("Manual.md")
    runner = passes(store, 0.0)
    for now, source, destination in ((60.0, first, second), (120.0, second, first), (180.0, first, second)):
        source.rename(destination)
        until(lambda now=now: runner.run(store, now=now) > 0)
        until(lambda now=now: runner.run(store, now=now + SETTLED) > 0)
        assert not source.exists() and destination.exists()
    assert len(txns(store, "view_move")) == 3
    assert store.connection.execute("SELECT row_version FROM items").fetchone() == (1,)
    assert len({request for _, _, request in txns(store, "view_move")}) == 3


def test_duplicate_current_stamp_destinations_are_held_instead_of_chosen_by_order(store):
    """Two detached copies of the current item cannot become a unique move through window eviction."""
    _, old = appended(store)
    store.reconcile_views()
    raw = old.read_bytes()
    one, two = old.with_name("CopyA.md"), old.with_name("CopyB.md")
    old.unlink()
    one.write_bytes(raw)
    two.write_bytes(raw)
    runner = edit_back.EditBack()
    runner.hints(store, (), True)
    for now in (0.0, SETTLED, 2 * SETTLED):
        until(lambda now=now: runner.run(store, now=now, limit=1) > 0)
    assert txns(store, "view_move") == []
    assert one.read_bytes() == two.read_bytes() == old.read_bytes() == raw
    assert len([code for _, code, _ in holds(store) if code == edit_back.CONFLICT]) == 2


def test_a_copied_stamp_cannot_release_different_detached_bytes_after_path_adoption(store, monkeypatch):
    """A recycled persisted inode and copied stamp must not release unrelated bytes after path adoption."""
    _, old = appended(store)
    canonical = old.read_bytes()
    detached = canonical.replace(b"title: One", b"title: Detached")
    destination = old.with_name("Manual.md")
    old.unlink()
    destination.write_bytes(detached)
    # A stale persisted identity can match an unrelated replacement after inode reuse.
    stat = destination.stat()
    store.connection.execute("UPDATE projection_state SET stat_identity=? WHERE kind='item'",
                             (f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}",))
    store.connection.commit()
    from test_governance_egress import _external

    from exomem import commands, held_fs
    from exomem.collection_store import governance, views
    from exomem.collection_store.preview import preview_store
    from exomem.governance import raw_protection
    from exomem.governance.principal import owner_principal, request_scope

    original = views.PublicationBatch._install
    with monkeypatch.context() as blocked:
        def interrupted(batch, publication):
            raise held_fs.HeldFsError("IO_ERROR", "injected installation interruption")

        blocked.setattr(views.PublicationBatch, "_install", interrupted)
        passes(store, 0.0, SETTLED, 2 * SETTLED, 3 * SETTLED)
        assert destination.read_bytes() == detached and txns(store, "view_edit") == []
        newer = detached.replace(b"title: Detached", b"title: Second save")
        destination.write_bytes(newer)
        passes(store, 60.0, 60.0 + SETTLED, 60.0 + 2 * SETTLED, 60.0 + 3 * SETTLED)
        assert destination.read_bytes() == newer and txns(store, "view_edit") == []
    assert views.PublicationBatch._install is original
    store.reconcile_views()
    assert destination.read_bytes() == canonical and not old.exists()
    assert store.connection.execute("SELECT row_version FROM items").fetchone() == (1,)
    assert set(store.connection.execute("SELECT held_bytes,quarantined FROM held_candidates")) == {(detached, 1), (newer, 1)}
    assert len(txns(store, "view_move")) == 1 and txns(store, "view_edit") == []
    held_paths = store.connection.execute("SELECT view_path,held_bytes FROM held_candidates").fetchall()
    with preview_store(store.root, store.handle), request_scope(owner_principal()):
        for held_path, raw in held_paths:
            assert raw.decode() in commands.op_get(store.root, path=held_path)["body"]
    for who, unrestricted in ((_external(), True), (owner_principal(), False)):
        with monkeypatch.context() as access:
            access.setattr(raw_protection, "has_unrestricted_access", lambda root, principal, value=unrestricted: value)
            with request_scope(who), store.read_snapshot():
                target = next(subject for subject in governance.subjects(store.connection, CID, "test")
                              if isinstance(subject.row_id, int))
                assert store._operation.decision(target).level == 6
            for held_path, _ in held_paths:
                with preview_store(store.root, store.handle), request_scope(who), pytest.raises(ValueError, match="NOT_FOUND"):
                    commands.op_get(store.root, path=held_path)


def test_discard_and_restart_do_not_release_an_unconfirmed_adopted_path(store, monkeypatch):
    """Discard cannot erase unresolved adoption; canonical confirmation must allow later ordinary saves again."""
    from exomem import held_fs
    from exomem.collection_store import connection, views
    from exomem.collection_store.writer import CollectionWriter

    _, old = appended(store)
    canonical = old.read_bytes()
    raw = canonical.replace(b"title: One", b"title: Detached")
    destination = old.with_name("Manual.md")
    old.rename(destination)
    destination.write_bytes(raw)
    with monkeypatch.context() as blocked:
        def interrupted(batch, publication):
            raise held_fs.HeldFsError("IO_ERROR", "injected installation interruption")

        blocked.setattr(views.PublicationBatch, "_install", interrupted)
        passes(store, 0.0, SETTLED, 2 * SETTLED)
        [(reference, _, _)] = holds(store)
        store.discard_held(CID, held=reference, why="discard this observation")
        assert holds(store) == [] and destination.read_bytes() == raw
        database, root = store.handle.path, store.root
        store.handle.close()
        with connection.open_writer(database, lease_check=lambda: True) as handle:
            resumed = CollectionWriter(root, handle)
            newer = raw.replace(b"title: Detached", b"title: After restart")
            destination.write_bytes(newer)
            passes(resumed, 0.0, SETTLED, 2 * SETTLED)
            assert txns(resumed, "view_edit") == [] and destination.read_bytes() == newer
            assert resumed.connection.execute("SELECT held_bytes,quarantined FROM held_candidates").fetchall() == [(newer, 1)]
    with connection.open_writer(database, lease_check=lambda: True) as handle:
        resumed = CollectionWriter(root, handle)
        resumed.reconcile_views()
        resumed.reconcile_views()  # This transaction confirms canonical bytes, rather than trusting _install's return.
        assert destination.read_bytes() == canonical
        with monkeypatch.context() as blocked:
            blocked.setattr(views.PublicationBatch, "_install", interrupted)
            container_hash, item_hash = resumed.current_guards(CID, KEY)
            resumed.update_record(CID, item_key=KEY, changes={"count": 2}, why="ordinary pending write",
                expected_container_hash=container_hash, expected_item_version=item_hash)
            current = resumed.connection.execute("SELECT values_json FROM items").fetchone()[0]
        resumed.reconcile_views()
        destination.write_bytes(destination.read_bytes().replace(b"title: One", b"title: Ordinary edit"))
        passes(resumed, 0.0, SETTLED)
        assert len(txns(resumed, "view_edit")) == 1
        assert json.loads(current) == {"count": 2, "title": "One"}
        assert json.loads(resumed.connection.execute("SELECT values_json FROM items").fetchone()[0]) == {
            "count": 2, "title": "Ordinary edit"}


def test_an_unbound_resume_adopts_its_observed_filename_and_refuses_a_later_edit(store):
    """Explicit owner adoption keeps a manual filename; a stale adoption preserves both observations."""
    create(store)
    source = store.root / "Knowledge Base/Records/Work/Items/Manual.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    original = b"---\ntitle: Detached\ncount: 1\n---\nAuthored text\n"
    source.write_bytes(original)
    passes(store, 0.0, SETTLED)
    [(held, code, raw)] = holds(store)
    assert (code, raw) == (edit_back.UNBOUND, original)
    changed = original.replace(b"count: 1", b"count: 2")
    source.write_bytes(changed)
    with pytest.raises(collections.CollectionError, match="VIEW_INPUT_CHANGED"):
        store.append_record(CID, held=held, why="adopt")
    assert {raw for _, _, raw in holds(store)} == {original, changed}
    assert store.connection.execute("SELECT count(*) FROM items").fetchone() == (0,)
    newer = next(reference for reference, _, raw in holds(store) if raw == changed)
    receipt = store.append_record(CID, held=newer, why="adopt current file")
    assert receipt["affected_paths"] == [source.relative_to(store.root).as_posix()]
    assert store.connection.execute("SELECT row_version,values_json FROM items").fetchone() == (
        1, json.dumps({"count": 2, "title": "Detached"}, separators=(",", ":")))
    assert len(txns(store, "append")) == 1 and [(code, raw) for _, code, raw in holds(store)] == [(edit_back.UNBOUND, original)]


def test_an_interrupted_census_cannot_confirm_absence_or_recreate_an_old_path(store, monkeypatch):
    """A directory read failure after seeing a candidate leaves its old path absent and reports incomplete work."""
    from exomem import held_fs

    _, old = appended(store)
    store.reconcile_views()
    destination = old.with_name("Manual.md")
    old.rename(destination)
    with held_fs.acquire(store.root).require() as fs:
        backend = type(fs)
    original = backend.iter_names

    def interrupted(fs, parent):
        yield from original(fs, parent)
        raise held_fs.HeldFsError("IO_ERROR", "injected directory interruption")

    monkeypatch.setattr(backend, "iter_names", interrupted)
    runner = passes(store, 0.0, SETTLED, 2 * SETTLED)
    store.reconcile_views(missing_ready=store.handle.census(store.root).missing_ready)
    assert destination.exists() and not old.exists() and txns(store, "view_move") == []
    assert runner.last_status["status"] == "incomplete" and runner.last_status["failures"] > 0


def test_an_oversized_candidate_keeps_all_bytes_and_prevents_an_absence_claim(store):
    """The existing parser byte bound must not truncate a correction or make a partial sweep look complete."""
    from exomem import record_formats

    _, old = appended(store)
    store.reconcile_views()
    destination = old.with_name("Manual.md")
    old.rename(destination)
    oversized = old.with_name("Oversized.md")
    raw = b"x" * (record_formats._MAX_ITEM_BYTES + 1)
    oversized.write_bytes(raw)
    runner = passes(store, 0.0, SETTLED)
    store.reconcile_views(missing_ready=store.handle.census(store.root).missing_ready)
    assert oversized.read_bytes() == raw and destination.exists() and not old.exists()
    assert holds(store) == [] and runner.last_status["status"] == "incomplete"


def test_a_duplicate_arriving_after_the_sweep_preserves_both_files_and_is_corrected_next_pass(store, monkeypatch):
    """A completed sweep is observational: an unseen late copy cannot make publication destroy either file."""
    from exomem.collection_store.writer import CollectionWriter

    _, old = appended(store)
    canonical = old.read_bytes()
    chosen, late = old.with_name("Manual.md"), old.with_name("Late.md")
    old.rename(chosen)
    original = CollectionWriter.reconcile_detached_candidate

    def arriving(writer, facts, **kwargs):
        if not late.exists():
            late.write_bytes(canonical)
        return original(writer, facts, **kwargs)

    monkeypatch.setattr(CollectionWriter, "reconcile_detached_candidate", arriving)
    runner = passes(store, 0.0, SETTLED)
    assert chosen.read_bytes() == late.read_bytes() == canonical and not old.exists()
    until(lambda: runner.run(store, now=60.0) > 0)
    until(lambda: runner.run(store, now=60.0 + SETTLED) > 0)
    assert chosen.read_bytes() == late.read_bytes() == canonical and len(txns(store, "view_move")) == 1
    assert [(code, raw) for _, code, raw in holds(store)] == [(edit_back.CONFLICT, canonical)]


def test_a_directory_rename_with_a_pending_install_and_changed_cleanup_input_preserves_all_bytes(store, monkeypatch):
    """A pending installation must preserve its moved edit, unseen aside and a stage changed during cleanup."""
    from exomem import held_fs
    from exomem.collection_store import views

    receipt, old = appended(store)
    nested = old.parent / "Nested" / "Manual.md"
    nested.parent.mkdir()
    old.rename(nested)
    passes(store, 0.0, SETTLED)
    store.reconcile_views()
    passes(store, 0.0)
    original = views.PublicationBatch._install

    def blocked(batch, publication):
        raise held_fs.HeldFsError("IO_ERROR", "injected installation interruption")

    monkeypatch.setattr(views.PublicationBatch, "_install", blocked)
    update = store.update_record(CID, item_key=KEY, changes={"count": 2}, why="update during edit",
        expected_container_hash=store.current_guards(CID)[0], expected_item_version=receipt["after_item_hash"])
    store.reconcile_views()
    monkeypatch.setattr(views.PublicationBatch, "_install", original)
    descriptor = json.loads(store.connection.execute("SELECT install_json FROM projection_state WHERE kind='item'").fetchone()[0])
    stage = nested.parent / (views.STAGE_PREFIX + descriptor["token"])
    edited = stage.read_bytes().replace(b"title: One", b"title: Owner")
    nested.write_bytes(edited)
    displaced = b"Unseen displaced input\n"
    (nested.parent / (views.ASIDE_PREFIX + descriptor["token"] + "-0")).write_bytes(displaced)
    renamed = nested.parent.with_name("Renamed")
    nested.parent.rename(renamed)
    destination = renamed / nested.name
    changed_stage = renamed / stage.name
    unseen = b"Stage changed after move preparation\n"
    publish = views.PublicationBatch.publish

    def changed_during_cleanup(batch):
        if any(leaf == stage.name for _projection, _parent, leaf, _digest in batch.cleanup):
            changed_stage.write_bytes(unseen)
        return publish(batch)

    monkeypatch.setattr(views.PublicationBatch, "publish", changed_during_cleanup)
    runner = passes(store, 0.0, SETTLED)
    assert runner.last_status["status"] == "current"
    assert not nested.exists() and not nested.parent.exists() and destination.exists()
    assert store.connection.execute("SELECT row_version,values_json FROM items").fetchone() == (
        2, json.dumps({"count": 2, "title": "One"}, separators=(",", ":")))
    assert {(code, raw) for _, code, raw in holds(store)} == {
        (edit_back.CONFLICT, displaced), (edit_back.CONFLICT, edited)}
    assert store.connection.execute("SELECT DISTINCT quarantined FROM held_candidates").fetchall() == [(1,)]
    assert len(txns(store, "view_move")) == 2 and txns(store, "view_edit") == []
    assert update["after_item_hash"] == store.current_guards(CID, KEY)[1] and destination.read_bytes() != edited
    monkeypatch.setattr(views.PublicationBatch, "publish", publish)
    for _ in range(2):
        result = store.reconcile_views()
        assert "projection_pending" in result["warnings"] and changed_stage.read_bytes() == unseen
    assert not nested.parent.exists() and list(renamed.glob(".exomem-collection-*")) == [changed_stage]


@pytest.mark.skipif(os.name == "nt", reason="POSIX redundant stage alias crash seam")
def test_a_refused_move_commit_keeps_unknown_stage_aliases_until_the_hold_is_durable(store, monkeypatch):
    """A refused COMMIT must not retire journal bytes before their quarantine commits."""
    from exomem import held_fs
    from exomem.collection_store import views

    _, old = appended(store)
    source = old.parent / "Nested" / "Manual.md"
    source.parent.mkdir()
    old.rename(source)
    passes(store, 0.0, SETTLED)
    store.reconcile_views()
    with monkeypatch.context() as blocked:
        def interrupted(batch, publication):
            raise held_fs.HeldFsError("IO_ERROR", "injected installation interruption")

        blocked.setattr(views.PublicationBatch, "_install", interrupted)
        store.update_record(CID, item_key=KEY, changes={"count": 2}, why="agent",
                            expected_container_hash=store.current_guards(CID)[0],
                            expected_item_version=store.current_guards(CID, KEY)[1])
        store.reconcile_views()
    descriptor = json.loads(store.connection.execute("SELECT install_json FROM projection_state WHERE kind='item'").fetchone()[0])
    stage = source.parent / (views.STAGE_PREFIX + descriptor["token"])
    unknown = stage.read_bytes().replace(b"title: One", b"title: Unknown")
    source.unlink()
    os.link(stage, source)
    source.write_bytes(unknown)
    renamed = source.parent.with_name("Renamed")
    source.parent.rename(renamed)
    destination, alias = renamed / source.name, renamed / stage.name
    moving = [False]

    def refuse_commit(action, name, column, *_):
        if action == sqlite3.SQLITE_UPDATE and name == "items" and column == "view_path":
            moving[0] = True
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION and name == "COMMIT" and moving[0] else sqlite3.SQLITE_OK

    store.connection.set_authorizer(refuse_commit)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            passes(store, 60.0, 60.0 + SETTLED)
    finally:
        store.connection.set_authorizer(None)
    assert alias.read_bytes() == destination.read_bytes() == unknown
    assert store.connection.execute("SELECT 1 FROM held_candidates WHERE held_bytes=?", (unknown,)).fetchone() is None
    from exomem.collection_store.writer import CollectionWriter

    advance = CollectionWriter._advance
    newer = unknown.replace(b"title: Unknown", b"title: Newer")
    with monkeypatch.context() as writing:
        def edit_before_precommit(writer, txn):
            if txn["operation"] == "view_move":
                destination.write_bytes(newer)
            return advance(writer, txn)

        writing.setattr(CollectionWriter, "_advance", edit_before_precommit)
        runner = passes(store, 120.0, 120.0 + SETTLED)
    assert runner.last_status["status"] == "incomplete" and alias.read_bytes() == destination.read_bytes() == newer
    assert store.connection.execute("SELECT 1 FROM held_candidates WHERE held_bytes=?", (newer,)).fetchone() is None
    passes(store, 180.0, 180.0 + SETTLED)
    assert not alias.exists() and destination.read_bytes() != unknown
    assert store.connection.execute("SELECT held_bytes,quarantined FROM held_candidates").fetchall() == [(newer, 1)]


def test_a_move_out_of_the_source_keeps_destination_bytes_and_holds_the_governed_copy(store):
    """A reported moved-out item keeps ordinary destination custody while its canonical path is restored."""
    _, old = appended(store)
    store.reconcile_views()
    original = old.read_bytes()
    destination = store.root / "Knowledge Base/Manual.md"
    old.rename(destination)
    runner = edit_back.EditBack()
    runner.hints(store, [(old.relative_to(store.root).as_posix(), destination.relative_to(store.root).as_posix(), False)])
    for now in (0.0, SETTLED, 2 * SETTLED):
        until(lambda now=now: runner.run(store, now=now) > 0)
    assert destination.read_bytes() == old.read_bytes() == original
    assert txns(store, "view_move") == []
    assert store.connection.execute("SELECT held_bytes,quarantined FROM held_candidates WHERE held_bytes<>?", (b"",)).fetchall() == [(original, 0)]
    assert [code for _, code, raw in holds(store) if raw] == [edit_back.MOVED]


def test_a_large_summary_census_reads_actual_files_without_generating_or_visiting_row_paths(store, monkeypatch):
    """A summary's canonical row count must not determine its census or filesystem work."""
    from test_collection_store_summary import _seed, summary_text

    from exomem import held_fs
    from exomem.collection_store import summary

    monkeypatch.setattr(capability, "RELEASED", capability.RELEASED | {capability.RECORDS_SUMMARY_V1})
    store.create_collection(manifest_path(), summary_text(), why="create", scaffold=False)
    _seed(store, CID, 100_001)
    store.reconcile_views()
    before = list(store.root.rglob("*.md"))
    with held_fs.acquire(store.root).require() as fs:
        backend = type(fs)
    reads, directories = [], []
    original_read, original_names = backend.read, backend.iter_names

    def read(fs, file, **kwargs):
        reads.append(file.version().size)
        return original_read(fs, file, **kwargs)

    def names(fs, parent):
        directories.append(parent)
        yield from original_names(fs, parent)

    monkeypatch.setattr(backend, "read", read)
    monkeypatch.setattr(backend, "iter_names", names)
    runner = passes(store, 0.0, SETTLED)
    assert runner.last_status["status"] == "current" and directories == []
    assert len(reads) <= summary.MAX_PAGES + 1 and max(reads) <= summary.MAX_PAGE_BYTES
    assert list(store.root.rglob("*.md")) == before
    assert store.connection.execute("SELECT count(*) FROM items WHERE view_path IS NOT NULL").fetchone() == (0,)


def test_one_window_reads_an_open_collection_beside_a_fenced_one(store, monkeypatch):
    """Defect: a closed route fence on one collection decides for every collection in its window."""
    from test_collection_store_summary import summary_text

    fenced = "88888888-8888-4888-8888-888888888888"
    create(store)
    daily = summary_text(manifest_text().replace(CID, fenced).replace("title: Work", "title: Daily"))
    store.create_collection(manifest_path().replace("Work", "Daily"), daily, why="create", scaffold=False)
    store.reconcile_views()
    paths = {row[0] for row in store.connection.execute("SELECT path FROM projection_state WHERE kind='manifest'")}
    assert len(paths) == 2

    def read_collections():
        return {found.projection["collection_id"] for found in edit_back.observe(store, force=paths)}

    assert read_collections() == {CID, fenced}
    monkeypatch.setattr(capability, "RELEASED", capability.RELEASED - {capability.RECORDS_SUMMARY_V1})
    assert read_collections() == {CID}


def test_an_invalid_save_is_held_once_and_stays_in_place(store):
    """Defect: one invalid save gives a second hold on the next reconcile pass, or loses its bytes."""
    _, path = appended(store, count=1)
    invalid = path.read_bytes().replace(b"count: 1", b"count: broken")
    path.write_bytes(invalid)
    store.reconcile_views()
    store.reconcile_views()
    assert [(code, raw) for _, code, raw in holds(store)] == [("VIEW_INVALID", invalid)]
    assert path.read_bytes() == invalid  # PROVISIONAL O2: kept in place until discarded
    from exomem import audit
    from exomem.collection_store.preview import preview_store

    with preview_store(store.root, store.handle):
        report = audit.audit(store.root, categories=["collection_view_corrections"])
    assert [finding.meta["code"] for finding in report.findings] == ["VIEW_INVALID"]


def test_an_invalid_view_waiting_to_be_held_does_not_spin_the_store_thread(store):
    """Defect: between settling and being held, an invalid view asks for a pass at once, every pass."""
    _, path = appended(store, count=1)
    path.write_bytes(path.read_bytes().replace(b"count: 1", b"count: broken"))
    runner = passes(store, 0.0)
    assert runner.run(store, now=SETTLED) == pytest.approx(HELD - SETTLED)
    assert holds(store) == []
    assert runner.run(store, now=HELD) > 0 and [code for _, code, _ in holds(store)] == ["VIEW_INVALID"]


def test_a_deleted_view_is_held_and_rendered_again(store):
    """Defect: a deleted view is silently rendered again, so its owner never learns no delete exists."""
    receipt, path = appended(store)
    store.reconcile_views()
    path.unlink()
    store.reconcile_views()
    assert [code for _, code, _ in holds(store)] == ["VIEW_DELETED"]
    assert get_page.get_page(store.root, path=receipt["affected_paths"][0]).frontmatter["title"] == "One"


def test_discard_resolves_a_view_correction_and_renders_over_its_bytes(store):
    """Defect: a held view correction cannot be discarded (C10), or its discarded bytes stay at the view."""
    receipt, path = appended(store, count=1)
    rendered = path.read_bytes()
    path.write_bytes(rendered.replace(b"count: 1", b"count: broken"))
    store.reconcile_views()
    [(held, code, _)] = holds(store)
    assert code == "VIEW_INVALID"
    discarded = store.discard_held(CID, held=held, why="drop the broken edit")
    assert discarded["outcome"] == "discarded" and holds(store) == []
    store.reconcile_views()
    assert path.read_bytes() == rendered and not list(path.parent.glob(".exomem-collection-aside-*"))
    with pytest.raises(collections.CollectionError, match="HELD_NOT_FOUND"):
        store.discard_held(CID, held=held, why="again")


def test_a_planning_hierarchy_violation_is_held_not_applied(store):
    """Defect: an edit that breaks the Planning hierarchy is classified without the graph and never held."""
    from exomem import vault

    create(store, "planning")
    receipt = store.append_record(CID, item={"title": "Child"}, item_key=KEY, why="capture")
    store.reconcile_views()
    path = store.root / receipt["affected_paths"][0]
    frontmatter, body, _ = vault.parse_frontmatter(path.read_text(), strict=True)
    # A committed work item needs a parent; this one has none.
    frontmatter.update(status="planned", commitment="committed", horizon="week")
    edited = ("---\n" + vault.serialize_frontmatter(frontmatter) + "\n---\n" + body).encode()
    path.write_bytes(edited)
    passes(store, 0.0, SETTLED, HELD)
    assert txns(store, "view_edit") == []
    [(_, code, raw)] = holds(store)
    assert (code, raw) == ("VIEW_INVALID", edited)
    diagnostics = json.loads(store.connection.execute("SELECT diagnostics_json FROM held_candidates").fetchone()[0])
    assert diagnostics[0]["issues"][0]["code"] == "INVALID_PLAN_RELATION"
    # Planning inspection names the correction while keeping its exact key set (design §8 C4).
    from exomem.collection_store.preview import preview_store
    from exomem.plan_memory import plan_memory

    with preview_store(store.root, store.handle):
        inspected = plan_memory(store.root, "inspect", collection=CID)
    assert "VIEW_INVALID" in {diagnostic["code"] for diagnostic in inspected["diagnostics"]}
    assert "projection" not in inspected and "coverage" not in inspected


def test_without_the_edit_back_release_a_hierarchy_violation_waits_on_disk(store, monkeypatch):
    """Defect: with edit-back unreleased, a Planning edit that breaks the hierarchy is held and its
    view rendered again, where a store before edit-back left it waiting on disk."""
    from exomem import vault

    monkeypatch.setattr(capability, "RELEASED", capability.RELEASED - {VIEWS})
    create(store, "planning")
    receipt = store.append_record(CID, item={"title": "Child"}, item_key=KEY, why="capture")
    store.reconcile_views()
    path = store.root / receipt["affected_paths"][0]
    frontmatter, body, _ = vault.parse_frontmatter(path.read_text(), strict=True)
    frontmatter.update(status="planned", commitment="committed", horizon="week")  # committed, with no parent
    edited = ("---\n" + vault.serialize_frontmatter(frontmatter) + "\n---\n" + body).encode()
    path.write_bytes(edited)
    store.reconcile_views()
    store.reconcile_views()
    assert holds(store) == [] and path.read_bytes() == edited
    assert store.connection.execute("SELECT state FROM projection_state WHERE path=?",
                                    (receipt["affected_paths"][0],)).fetchone() == ("pending",)


def test_a_manifest_view_edit_becomes_a_revise(store):
    """Defect: an edit of the manifest view is ignored, or bypasses the revise rules."""
    create(store)
    path = store.root / manifest_path()
    path.write_bytes(path.read_bytes().replace(b"title: Work", b"title: Revised work"))
    passes(store, 0.0, SETTLED)
    assert txns(store, "view_edit") == [("view_edit", "owner:view-edit", txns(store, "view_edit")[0][2])]
    version, text = store.connection.execute(
        "SELECT m.manifest_version,m.manifest_text FROM collections c JOIN collection_manifests m "
        "ON m.collection_id=c.collection_id AND m.manifest_version=c.manifest_version").fetchone()
    assert version == 2 and "title: Revised work" in text and "exomem_view" not in text
    assert get_page.get_page(store.root, path=manifest_path()).frontmatter["exomem_view"]["v"] == 2


def test_a_formatting_only_edit_is_rendered_again_without_a_transaction(store):
    """Defect: re-quoting a value or reordering properties commits a transition or leaves the edit."""
    from exomem import vault

    _, path = appended(store, count=1)
    rendered = path.read_bytes()
    frontmatter, body, _ = vault.parse_frontmatter(rendered.decode(), strict=True)
    reordered = dict(reversed(list(frontmatter.items())))
    path.write_bytes(("---\n" + vault.serialize_frontmatter(reordered) + "\n---\n" + body).encode())
    before = txns(store)
    passes(store, 0.0, SETTLED)
    assert txns(store) == before and holds(store) == []
    assert path.read_bytes() == rendered


def test_a_typing_storm_leaves_no_hold(store):
    """Defect: an editor's autosaves, invalid between keystrokes, are held before the owner finishes."""
    _, path = appended(store, count=1)
    rendered = path.read_bytes()
    runner = edit_back.EditBack()
    now = 0.0
    for keystroke in range(12):  # 18 s of autosaves, each followed by a half-second pause
        path.write_bytes(rendered.replace(b"count: 1", f"count: 1{keystroke}x".encode()))
        runner.notify(store, [path.relative_to(store.root).as_posix()])
        until(lambda now=now: runner.run(store, now=now) > 0)
        until(lambda now=now: runner.run(store, now=now + 0.5) > 0)
        now += 1.5
    assert holds(store) == []
    path.write_bytes(rendered.replace(b"count: 1", b"count: 12"))
    runner.notify(store, [path.relative_to(store.root).as_posix()])
    until(lambda now=now: runner.run(store, now=now) > 0)
    until(lambda now=now: runner.run(store, now=now + 2.0) > 0)
    assert holds(store) == [] and len(txns(store, "view_edit")) == 1
    assert json.loads(store.connection.execute("SELECT values_json FROM items").fetchone()[0])["count"] == 12


# The installed service, no watcher: a migrated vault whose Records collection the store serves.

WORK = manifest_path()
SECRET_TAGS = "    tags: {type: array, items: {type: string}}\n"


class Served:
    """A migrated vault and the service serving it, which a test may stop and start again."""

    def __init__(self, root):
        self.root = root
        self.service = None

    def start(self):
        from test_collection_store_migration import start_service

        self.service = start_service(self.root)

    def stop(self):
        self.service.stop()
        self.service = None


@pytest.fixture
def served(env, capsys, monkeypatch):
    from test_collection_store_migration import migrate, stop_writer

    from exomem import records
    from exomem.init import init_vault

    # A full pass every few tenths of a second, so a case need not wait the production 60 s.
    monkeypatch.setattr(edit_back, "FULL_PASS_SECONDS", 0.3)
    root = env
    init_vault(root)
    records.create_collection(root, WORK, manifest_text().replace("    count:", SECRET_TAGS + "    count:"),
                              why="file fixture", scaffold=True)
    stop_writer()
    assert migrate(root, "apply") == 0
    monkeypatch.setattr(capability, "RELEASED", capability.RELEASED | {VIEWS})  # env set its own release
    capsys.readouterr()
    vault = Served(root)
    vault.start()
    try:
        yield vault
    finally:
        if vault.service is not None:
            vault.stop()


def call_as(principal, root, tool, **arguments):
    from exomem import commands, writer_lease
    from exomem.governance.principal import request_scope

    command = next(command for command in commands.product_commands_for("mcp") if command.name == tool)
    with request_scope(principal):
        return writer_lease.invoke_command(command, root, **arguments)


def owner(root, tool, **arguments):
    from exomem.governance.principal import owner_principal

    return call_as(owner_principal(surface="mcp"), root, tool, **arguments)


def store_rows(root, sql, parameters=()):
    from contextlib import closing

    from exomem.collection_store import connection

    with closing(connection.open_reader(connection.store_path(root))) as reader:
        return reader.execute(sql, parameters).fetchall()


def until(predicate, seconds=20):
    from test_collection_store_s1_gate import _until

    _until(predicate, seconds)


def published_view(root, title, **values):
    """Append through the service and wait until the tick has confirmed its view current."""
    receipt = owner(root, "record_memory", action="append", collection=CID, item={"title": title, **values},
                    why="capture")
    relative = receipt["affected_paths"][0]
    until(lambda: store_rows(root, "SELECT state FROM projection_state WHERE path=?", (relative,)) == [("current",)])
    return relative, root / relative


def counted(root, title):
    rows = owner(root, "record_memory", action="query", collection=CID)["rows"]
    return next(row.get("count") for row in rows if row["title"] == title)


def test_a_watcher_notification_never_opens_an_unserved_store(tmp_path, monkeypatch):
    """Defect: an ordinary vault event starts a store thread or opens a SQLite database."""
    from exomem.collection_store import connection, runtime

    def unexpected(*args, **kwargs):
        pytest.fail("a watcher notification opened a store")

    monkeypatch.setattr(runtime, "_server", unexpected)
    monkeypatch.setattr(connection, "open_reader", unexpected)
    root = tmp_path / "ordinary"
    runtime.notify_view_change(root, "notes/page.md")
    assert root.resolve() not in runtime._SERVERS and not root.exists()


def test_view_notification_overflow_keeps_bounded_move_hints_and_requests_a_census(service):
    """Defect: a sync burst grows retained event metadata or loses its overflow recovery signal."""
    from exomem.collection_store import runtime

    server = runtime._server(service.root)

    def burst():
        runtime.notify_view_change(service.root, "old", destination="new", directory=True)
        for index in range(runtime._VIEW_CHANGE_CAPACITY + 2):
            runtime.notify_view_change(service.root, f"unknown/{index}.md")
        hints, census = server.take_view_hints()
        assert len(hints) == runtime._VIEW_CHANGE_CAPACITY and census
        assert ("old", "new", True) in hints
        assert server.take_view_hints() == ((), False)

    server.call(burst, wake_maintenance=False)


def test_directory_dirtiness_lost_in_an_overflow_still_finds_a_moved_item_before_the_periodic_pass(served, monkeypatch):
    """A directory hint that overflows the watcher mailbox must still discover the moved file through enumeration."""
    from exomem import file_watcher
    from exomem.collection_store import runtime

    root = served.root
    monkeypatch.setattr(edit_back, "FULL_PASS_SECONDS", 60.0)
    server = runtime._server(root)
    until(lambda: server._edit_back is not None and server._edit_back._next_full > time.monotonic() + 50)
    _, old = published_view(root, "Renamed")
    destination = old.parent / "Nested" / "Manual.md"
    destination.parent.mkdir()

    def burst():
        for index in range(runtime._VIEW_CHANGE_CAPACITY):
            runtime.notify_view_change(root, f"unrelated/{index}.md")
        old.rename(destination)
        file_watcher.FileWatcher(root)._notify_view_change(old.parent, directory=True)
        assert server._view_census_needed

    server.call(burst, wake_maintenance=False)
    until(lambda: store_rows(root, "SELECT view_path FROM items") == [(destination.relative_to(root).as_posix(),)], seconds=8)
    assert not old.exists() and destination.exists()
    assert store_rows(root, "SELECT operation FROM txns WHERE operation='view_move'") == [("view_move",)]


def test_a_watcher_save_burst_with_restored_stat_applies_before_the_full_pass(served, monkeypatch):
    """Defect: a view created after the initial scan waits 60 s, or suppression hides changed bytes."""
    from exomem import file_watcher
    from exomem.collection_store import runtime

    root = served.root
    monkeypatch.setattr(edit_back, "FULL_PASS_SECONDS", 60.0)
    server = runtime._server(root)
    until(lambda: server._edit_back is not None and server._edit_back._next_full > time.monotonic() + 50)
    _, path = published_view(root, "Watched", count=1)
    original, info = path.read_bytes(), path.stat()
    watcher = file_watcher.FileWatcher(root)
    # The index watcher can suppress a matching stat signature; edit-back must still see the save.
    file_watcher.register_self_write(root, [path])
    for count in (b"x", b"3", b"2"):
        path.write_bytes(original.replace(b"count: 1", b"count: " + count))
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        assert file_watcher._is_self_write_event(root, path, deleted=False)
        watcher._record(path, deleted=False)
        time.sleep(0.05)
    until(lambda: store_rows(root, "SELECT row_version FROM items") == [(2,)], seconds=6)
    assert counted(root, "Watched") == 2
    assert store_rows(root, "SELECT operation FROM txns WHERE operation='view_edit'") == [("view_edit",)]
    assert store_rows(root, "SELECT code FROM held_candidates WHERE kind='view-correction'") == []
    assert server._edit_back._next_full > time.monotonic() + 40


def test_pending_view_publication_yields_to_a_waiting_query(served, monkeypatch):
    """A pending view window must release a reader before it starts another slow path."""
    from exomem.collection_store import runtime
    from exomem.collection_store.writer import CollectionWriter

    root = served.root
    server = runtime._server(root)
    publish = threading.Event()
    first_render = threading.Event()
    continue_first = threading.Event()
    continue_later = threading.Event()
    query_done = threading.Event()
    query_result, errors, first_path = [], [], []
    tick = runtime.StoreServer._views_tick

    def paused_tick(current):
        return tick(current) if publish.is_set() else 0.05

    monkeypatch.setattr(runtime.StoreServer, "_views_tick", paused_tick)
    server.call(lambda: None, wake_maintenance=False)
    for title in ("First", "Second"):
        owner(root, "record_memory", action="append", collection=CID,
              item={"title": title, "count": 1}, why="capture")
    before = store_rows(root, "SELECT txn_id FROM txns ORDER BY txn_id")
    render = CollectionWriter._render_view

    def slow_later_path(writer, projection, manifest):
        if threading.current_thread() is server.thread and projection["kind"] == "item":
            if not first_path:
                first_path.append(projection["path"])
                first_render.set()
                assert continue_first.wait(10), "the first publication was not released"
            elif projection["path"] != first_path[0]:
                assert continue_later.wait(10), "the later publication was not released"
        return render(writer, projection, manifest)

    def query():
        try:
            query_result.append(owner(root, "record_memory", action="query", collection=CID))
        except Exception as error:
            errors.append((type(error).__name__, getattr(error, "code", None),
                           getattr(error, "message", None), getattr(error, "details", None)))
        finally:
            query_done.set()

    reader = threading.Thread(target=query)
    monkeypatch.setattr(CollectionWriter, "_render_view", slow_later_path)
    try:
        publish.set()
        assert first_render.wait(10), "pending item publication did not start"
        reader.start()
        # The store thread is held in the first render, so the only queued request is the query's.
        until(lambda: server._requests, seconds=10)
        continue_first.set()
        assert query_done.wait(10), "the foreground query did not complete"
        if errors:
            print(json.dumps({"view_publication_query_refusal": errors}, sort_keys=True), flush=True)
        assert not errors, errors
        assert sorted(row["title"] for row in query_result[0]["rows"]) == ["First", "Second"]
        assert store_rows(root, "SELECT COUNT(*) FROM projection_state WHERE kind='item' AND state='pending'")[0][0] > 0
    finally:
        continue_first.set()
        continue_later.set()
        if reader.ident is not None:
            reader.join(10)
    assert not reader.is_alive()
    until(lambda: store_rows(root, "SELECT COUNT(*) FROM projection_state WHERE state='pending'") == [(0,)])
    assert store_rows(root, "SELECT txn_id FROM txns ORDER BY txn_id") == before


def test_a_views_tick_publishes_under_the_policy_it_finds_at_the_write(served, monkeypatch):
    """Defect: a tick publishes a view on the policy it read at its start, after the policy withheld
    the collection, or keeps its start-of-tick policy for the next tick."""
    from test_governance_egress import write_broken_policy

    from exomem.collection_store import history, runtime
    from exomem.collection_store.writer import CollectionWriter

    root = served.root
    published_view(root, "Settled", count=1)
    release = threading.Event()
    ticked = threading.Event()
    tick = runtime.StoreServer._views_tick

    def one_tick_per_release(current):
        if not release.is_set():
            return 0.05
        release.clear()
        try:
            return tick(current)
        finally:
            ticked.set()

    def run_one_tick():
        ticked.clear()
        release.set()
        assert ticked.wait(10), "the views tick did not run"

    monkeypatch.setattr(runtime.StoreServer, "_views_tick", one_tick_per_release)
    run_one_tick()  # a tick under the allowing policy records its history release inputs
    receipt = owner(root, "record_memory", action="append", collection=CID,
                    item={"title": "Withheld", "count": 1}, why="capture")
    relative = receipt["affected_paths"][0]
    assert store_rows(root, "SELECT state FROM projection_state WHERE path=?", (relative,)) == [("pending",)]
    inputs, invalidate = history.release_inputs, CollectionWriter.invalidate_history_pages
    withheld, invalidations = threading.Event(), []

    def withhold_after_the_tick_read_its_policy(vault_root):
        found = inputs(vault_root)
        if not withheld.is_set():
            withheld.set()
            write_broken_policy(vault_root)  # a blocked policy withholds every collection from the writer
        return found

    def counted_invalidation(writer):
        invalidations.append(withheld.is_set())
        return invalidate(writer)

    monkeypatch.setattr(history, "release_inputs", withhold_after_the_tick_read_its_policy)
    monkeypatch.setattr(CollectionWriter, "invalidate_history_pages", counted_invalidation)
    run_one_tick()
    assert withheld.is_set() and invalidations == []  # this tick still compares the policy it read first
    assert store_rows(root, "SELECT state FROM projection_state WHERE path=?", (relative,)) == [("pending",)]
    assert not (root / relative).exists()
    run_one_tick()  # the next tick reads the new policy, so it queues the history pages
    assert invalidations == [True]
    assert not (root / relative).exists()


def test_back_to_back_reads_never_wait_behind_due_maintenance(served, monkeypatch):
    """Defect: a tick starts between the reads of a busy client, or after every request, or never
    starts once the reads stop."""
    from exomem import foreground_priority
    from exomem.collection_store import runtime

    server = runtime._server(served.root)
    ticks, running = [], threading.Event()

    def slow_tick(current):
        running.set()
        started = time.monotonic()
        time.sleep(0.1)
        ticks.append((started, time.monotonic()))
        running.clear()
        return 0.0

    monkeypatch.setattr(runtime.StoreServer, "_views_tick", slow_tick)
    monkeypatch.setattr(runtime, "MAINTENANCE_DEFER_SECONDS", 10.0)
    until(lambda: ticks)  # the stand-in is due at once, so the first quiet gap runs it
    with foreground_priority.foreground():  # a client between its calls: the gate stays shut
        until(lambda: not running.is_set())
        burst_start = time.monotonic()
        for _ in range(30):
            server.call(lambda: None, read_only=True, wake_maintenance=False)
            time.sleep(0.005)
        burst_end = time.monotonic()
    assert [tick for tick in ticks if tick[0] < burst_end and burst_start < tick[1]] == []
    until(lambda: ticks[-1][0] > burst_end)
    assert ticks[-1][0] - burst_end < runtime.FOREGROUND_GRACE_SECONDS + 0.5


@pytest.mark.parametrize("demand", ["queued_requests", "foreground_mark"])
def test_continuous_demand_does_not_starve_a_notified_view_edit(served, monkeypatch, demand):
    """Defect: a busy service never opens its maintenance gate and so never applies a settled saved view."""
    from exomem import file_watcher, foreground_priority
    from exomem.collection_store import runtime
    from exomem.collection_store.writer import CollectionWriter

    root = served.root
    monkeypatch.setattr(edit_back, "FULL_PASS_SECONDS", 60.0)
    monkeypatch.setattr(runtime, "MAINTENANCE_DEFER_SECONDS", 0.2)
    server = runtime._server(root)
    until(lambda: server._edit_back is not None and server._edit_back._next_full > time.monotonic() + 50)
    apply_edit = CollectionWriter.apply_view_edit

    def edit_after_completed_publication_passes(writer, *args, **kwargs):
        # A settled edit can follow a completed wake while demand stays continuous.
        server._views_after, server._views_passes = None, 2
        return apply_edit(writer, *args, **kwargs)

    monkeypatch.setattr(CollectionWriter, "apply_view_edit", edit_after_completed_publication_passes)
    _, path = published_view(root, "Busy", count=1)
    demanding, released = threading.Event(), threading.Event()
    demanding.set()
    original_serve = server._serve
    holder = None
    marked = threading.Event()

    def keep_requests_queued(request):
        original_serve(request)
        if demanding.is_set():
            with server._condition:
                server._requests.append(runtime._Call(lambda: None, wake_maintenance=False))
                server._condition.notify_all()
            time.sleep(0.005)

    def hold_a_foreground_mark():
        with foreground_priority.foreground():
            marked.set()
            released.wait(30)

    if demand == "queued_requests":
        monkeypatch.setattr(server, "_serve", keep_requests_queued)
    else:
        holder = threading.Thread(target=hold_a_foreground_mark)
        holder.start()
        assert marked.wait(10)
    try:
        server.call(lambda: None, wake_maintenance=False)
        path.write_bytes(path.read_bytes().replace(b"count: 1", b"count: 2"))
        file_watcher.FileWatcher(root)._record(path, deleted=False)
        until(lambda: store_rows(root, "SELECT row_version FROM items") == [(2,)], seconds=8)
        until(lambda: store_rows(root, "SELECT state FROM projection_state WHERE path=?",
                                (path.relative_to(root).as_posix(),)) == [("current",)], seconds=8)
        assert b"count: 2" in path.read_bytes()
        assert demanding.is_set()
        assert store_rows(root, "SELECT operation FROM txns WHERE operation='view_edit'") == [("view_edit",)]
    finally:
        demanding.clear()
        released.set()
        if holder is not None:
            holder.join(10)


def test_a_view_edit_committed_beside_a_queued_request_re_renders_under_continuous_demand(served, monkeypatch):
    """Defect: a views tick that yields to a request before publishing leaves the edit's re-render
    unowed, so under continuous demand it waits for the next census."""
    from exomem import file_watcher
    from exomem.collection_store import runtime
    from exomem.collection_store.writer import CollectionWriter

    root = served.root
    monkeypatch.setattr(edit_back, "FULL_PASS_SECONDS", 60.0)
    monkeypatch.setattr(runtime, "MAINTENANCE_DEFER_SECONDS", 1.0)  # a tick that commits after a quiet gap is not starved
    server = runtime._server(root)
    until(lambda: server._edit_back is not None and server._edit_back._next_full > time.monotonic() + 50)
    relative, path = published_view(root, "Busy", count=1)
    demanding = threading.Event()
    apply_edit, original_serve = CollectionWriter.apply_view_edit, server._serve

    def queue_a_request():
        with server._condition:
            server._requests.append(runtime._Call(lambda: None, wake_maintenance=False))
            server._condition.notify_all()

    def edit_with_a_request_queued_at_the_commit(writer, *args, **kwargs):
        result = apply_edit(writer, *args, **kwargs)
        demanding.set()
        queue_a_request()  # the tick that committed the edit now sees a waiting request
        return result

    def keep_requests_queued(request):
        original_serve(request)
        if demanding.is_set():
            queue_a_request()
            time.sleep(0.005)

    monkeypatch.setattr(CollectionWriter, "apply_view_edit", edit_with_a_request_queued_at_the_commit)
    monkeypatch.setattr(server, "_serve", keep_requests_queued)
    try:
        path.write_bytes(path.read_bytes().replace(b"count: 1", b"count: 2"))
        file_watcher.FileWatcher(root)._record(path, deleted=False)
        until(demanding.is_set)
        assert store_rows(root, "SELECT row_version FROM items") == [(2,)]
        pending = store_rows(root, "SELECT COUNT(*) FROM projection_state WHERE state='pending'")[0][0]
        # Each starved turn publishes one path, a second pass confirms the view, and the census stays 60 s away.
        until(lambda: store_rows(root, "SELECT state FROM projection_state WHERE path=?", (relative,)) == [("current",)],
              seconds=(pending + 1) * runtime.MAINTENANCE_DEFER_SECONDS + 4)
        assert b"count: 2" in path.read_bytes()
        assert demanding.is_set()
    finally:
        demanding.clear()


def test_an_on_disk_item_edit_becomes_one_governed_view_edit_and_a_stale_save_is_held(served):
    """Defect: the installed service never applies an edit made in a view, applies it twice, loses a
    stale-buffer save, or holds it where inspect, attention and discard cannot reach it."""
    root = served.root
    relative, path = published_view(root, "Served", count=1)
    first = path.read_bytes()
    edited = first.replace(b"count: 1", b"count: 7")
    path.write_bytes(edited)
    until(lambda: counted(root, "Served") == 7)
    edits = "SELECT operation,actor FROM txns WHERE operation='view_edit'"
    assert store_rows(root, edits) == [("view_edit", "owner:view-edit")]
    def page_count():
        try:
            return owner(root, "read_memory", path=relative)["frontmatter"]["count"]
        except ValueError as error:
            # From the save until the re-render installs, the view's bytes are not the published ones and a
            # read answers NOT_FOUND. That window opens in the settle window, before the edit commits.
            if "NOT_FOUND" not in str(error):
                raise

    until(lambda: page_count() == 7)
    rendered = path.read_bytes()

    def view_is_current():
        try:
            return path.read_bytes() == rendered
        except FileNotFoundError:
            # Publication moves the old view aside before the no-clobber install.
            return False

    path.write_bytes(edited)  # the same save again
    until(view_is_current)
    assert len(store_rows(root, edits)) == 1
    path.write_bytes(first.replace(b"count: 1", b"count: 9"))  # a stale buffer, stamped before the edit
    until(lambda: owner(root, "record_memory", action="inspect", collection=CID)["projection"]
          ["held_view_corrections"] == 1)
    inspected = owner(root, "record_memory", action="inspect", collection=CID)
    assert "VIEW_CONFLICT" in {diagnostic["code"] for diagnostic in inspected["diagnostics"]}
    held = inspected["coverage"]["held_refs"][0]["held_id"]
    attention = owner(root, "review_memory", mode="attention")  # the default union
    assert held in json.dumps(attention)
    until(view_is_current)  # the view shows the store again
    owner(root, "record_memory", action="discard", collection=CID, held=held, why="a stale buffer")
    assert owner(root, "record_memory", action="inspect", collection=CID)["projection"]["held_view_corrections"] == 0
    assert held not in json.dumps(owner(root, "review_memory", mode="attention"))
    assert counted(root, "Served") == 7 and len(store_rows(root, edits)) == 1


def test_an_edit_made_while_the_service_is_stopped_is_applied_after_it_starts(served):
    """Defect: an offline edit is never classified, or the projector overwrites it at startup."""
    root = served.root
    _, path = published_view(root, "Offline", count=1)
    served.stop()
    path.write_bytes(path.read_bytes().replace(b"count: 1", b"count: 4"))
    served.start()
    until(lambda: counted(root, "Offline") == 4)
    assert store_rows(root, "SELECT COUNT(*) FROM held_candidates") == [(0,)]


def test_attention_for_a_withheld_view_correction_equals_attention_without_it(served):
    """Defect: the attention queue names a held correction of a row the caller cannot see."""
    from test_governance_egress import _external, write_rule, write_scope

    from exomem import audit
    from exomem.collection_store import authority
    from exomem.governance.principal import owner_principal, request_scope

    root = served.root
    published_view(root, "Public", count=1)
    _, path = published_view(root, "Secret", count=1, tags=["secret"])
    write_scope(root, paths="Unrelated/**")
    scope = root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + "tags: [secret]\n")
    write_rule(root, ceiling=0)

    def queue(principal):
        # The audit category the queue reads, and the queue the caller receives.
        with request_scope(principal):
            found = edit_back.attention_findings(root)
        return found, call_as(principal, root, "review_memory", mode="attention",
                              categories=["collection_view_corrections"])

    first = path.read_bytes()
    path.write_bytes(first.replace(b"count: 1", b"count: 2"))
    until(lambda: counted(root, "Secret") == 2)
    absent = queue(_external())
    path.write_bytes(first.replace(b"count: 1", b"count: 3"))  # stale: stamped before the edit
    until(lambda: owner(root, "record_memory", action="inspect", collection=CID)["projection"]
          ["held_view_corrections"] == 1)
    assert queue(_external()) == absent and absent[0] == ([], None)
    assert "VIEW_CONFLICT" in json.dumps(queue(owner_principal(surface="mcp"))[1])
    with request_scope(owner_principal(surface="mcp")):
        before = audit.audit(root, categories=["collection_view_corrections"])
    assert len(before.findings) == 1
    authority.marker_path(root).unlink()
    with request_scope(owner_principal(surface="mcp")):
        after = audit.audit(root, categories=["collection_view_corrections"])
    assert after.findings == before.findings
    assert "VIEW_CONFLICT" in json.dumps(queue(owner_principal(surface="mcp"))[1])
    assert queue(_external()) == absent
    served.stop()
    with request_scope(owner_principal(surface="mcp")):
        unavailable = audit.audit(root, categories=["collection_view_corrections"])
    assert unavailable.findings == []
    assert unavailable.metadata["coverage"]["collection_view_corrections"] == "unknown"


def test_a_held_stale_buffer_save_resumes_as_the_owners_edit_over_the_current_row(store):
    """Defect: a held VIEW_CONFLICT cannot be resumed, resuming reverts the newer row, or a refused
    resume drops the owner's bytes."""
    receipt, path = appended(store, count=1)
    store.reconcile_views()
    stale = path.read_bytes().replace(b"title: One", b"title: Owner")
    agent = store.update_record(CID, item_key=KEY, changes={"count": 2}, why="agent",
                                expected_container_hash=receipt["after_container_hash"],
                                expected_item_version=receipt["after_item_hash"])
    path.write_bytes(stale)
    passes(store, 0.0, SETTLED)
    [(held, code, raw)] = holds(store)
    assert (code, raw) == ("VIEW_CONFLICT", stale)
    with pytest.raises(collections.CollectionError, match="SCHEMA_FIELD_TYPE"):
        store.update_record(CID, item_key=KEY, held=held, changes={"count": "bad"}, why="resume",
                            expected_container_hash=agent["after_container_hash"],
                            expected_item_version=agent["after_item_hash"])
    assert holds(store) == [(held, "VIEW_CONFLICT", stale)]
    store.update_record(CID, item_key=KEY, held=held, why="keep the owner's title",
                        expected_container_hash=agent["after_container_hash"],
                        expected_item_version=agent["after_item_hash"])
    assert holds(store) == []
    assert json.loads(store.connection.execute("SELECT values_json FROM items").fetchone()[0]) == {
        "count": 2, "title": "Owner"}


def test_an_edit_inside_the_managed_presentation_block_is_not_adopted(store):
    """Defect: text typed into the generated presentation block becomes the item's authored body."""
    text = manifest_text().removesuffix("---\n") + "item_presentation:\n  version: 1\n  title: title\n  summary: [count]\n---\n"
    store.create_collection(manifest_path(), text, why="create", scaffold=False)
    receipt = store.append_record(CID, item={"title": "One", "count": 1}, item_key=KEY, why="capture",
                                  body="Authored line\n")
    store.reconcile_views()
    path = store.root / receipt["affected_paths"][0]
    rendered = path.read_bytes()
    assert b"- **Count:** 1\n" in rendered
    path.write_bytes(rendered.replace(b"- **Count:** 1\n", b"- **Count:** 99\n"))
    before = txns(store)
    passes(store, 0.0, SETTLED)
    assert txns(store) == before and holds(store) == [] and path.read_bytes() == rendered
    assert store.connection.execute("SELECT body FROM items").fetchone() == ("Authored line\n",)


def test_the_stores_own_render_is_left_alone_and_then_skipped_by_its_stat(store, monkeypatch):
    """Defect: the store's own render is taken for an edit, or every pass reads every view again."""
    from exomem import held_fs

    _, path = appended(store, count=1)
    store.reconcile_views()
    rendered, before = path.read_bytes(), txns(store)
    with held_fs.acquire(store.root).require() as fs:
        platform = type(fs)
    reads = []
    read = platform.read
    def observe_read(fs, file, **kwargs):
        reads.append(file)
        return read(fs, file, **kwargs)

    monkeypatch.setattr(platform, "read", observe_read)
    runner = passes(store, 0.0)
    assert len(reads) == 3  # the first pass reads the manifest, item and history views
    until(lambda: runner.run(store, now=60.0) > 0)
    assert len(reads) == 3  # the next full pass only stats them
    assert txns(store) == before and holds(store) == [] and path.read_bytes() == rendered


def test_views_render_no_file_mode_audit_marker_or_head(store):
    """Defect: a store view carries a file-mode audit marker or a migrated manifest's audit head,
    which no store write keeps current and an edit of the view would carry back."""
    from exomem import record_formats

    legacy = record_formats.render_manifest_audit_head(manifest_text(), f"{7:024x}", semantic_profile="records",
                                                       reader_version=1)
    assert "record_audit" in legacy
    store.create_collection(manifest_path(), legacy, why="a migrated manifest", scaffold=False)
    receipt = store.append_record(CID, item={"title": "One"}, item_key=KEY, why="observe")
    store.reconcile_views()
    assert b"exomem-record-audit" not in (store.root / receipt["affected_paths"][0]).read_bytes()
    manifest = get_page.get_page(store.root, path=manifest_path()).frontmatter
    assert "record_audit" not in manifest and manifest["exomem_view"]["v"] == 1


def vault_bytes(root):
    """Every vault file but the store replica, which publishes after earlier commits on its own schedule."""
    from exomem.collection_store import replica

    skipped = replica.replica_path(root)
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*"))
            if path.is_file() and path != skipped}


@pytest.fixture
def quick_service(monkeypatch, request):
    """The summary-collection service, started with edit-back windows of tenths of a second."""
    monkeypatch.setattr(edit_back, "SETTLE_SECONDS", 0.2)
    monkeypatch.setattr(edit_back, "INVALID_SECONDS", 0.4)
    monkeypatch.setattr(edit_back, "FULL_PASS_SECONDS", 0.3)
    return request.getfixturevalue("service")


@pytest.mark.parametrize("released", [frozenset(), frozenset({VIEWS})], ids=["production", "summary-fenced"])
def test_an_unreleased_route_takes_no_view_edit(quick_service, monkeypatch, released):
    """Defect: edit-back commits or holds an owner's edit of a summary collection's view while the
    release keeps edit-back off, or while it keeps every route on that collection refused."""
    import time

    from exomem.collection_store import runtime

    root, view = quick_service.root, quick_service.root / manifest_path()
    until(lambda: store_rows(root, "SELECT COUNT(*) FROM projection_state WHERE state!='current'") == [(0,)])
    monkeypatch.setattr(capability, "RELEASED", released)
    view.write_bytes(view.read_bytes().replace(b"title: Work", b"title: Renamed"))
    before, txns_before = vault_bytes(root), store_rows(root, "SELECT COUNT(*) FROM txns")
    for _ in range(6):  # each served request wakes the store thread's views tick
        runtime._server(root).call(lambda: None)
        time.sleep(0.5)
    assert store_rows(root, "SELECT COUNT(*) FROM txns") == txns_before
    assert store_rows(root, "SELECT COUNT(*) FROM held_candidates") == [(0,)]
    assert vault_bytes(root) == before
    # The same edit is applied once the release serves both edit-back and the collection.
    monkeypatch.setattr(capability, "RELEASED", frozenset({capability.RECORDS_SUMMARY_V1, VIEWS}))
    runtime._server(root).call(lambda: None)
    until(lambda: store_rows(root, "SELECT COUNT(*) FROM txns WHERE operation='view_edit'") == [(1,)])
    [(text,)] = store_rows(root, "SELECT manifest_text FROM collection_manifests ORDER BY manifest_version DESC LIMIT 1")
    assert "title: Renamed" in text


def test_an_owner_edit_cannot_turn_an_empty_summary_collection_into_an_items_mode_one(quick_service, monkeypatch):
    """Defect: under the edit-back release, an owner who removes view_mode from an empty summary
    collection's manifest view turns it into an items-mode store collection; only the route refused."""
    from test_collection_store_service import call
    from test_collection_store_summary import summary_text

    root, empty = quick_service.root, "88888888-8888-4888-8888-888888888888"
    path = manifest_path().replace("Work", "Empty")
    monkeypatch.setattr(capability, "RELEASED", frozenset({capability.RECORDS_SUMMARY_V1, VIEWS}))
    call(root, "record_memory", action="create", manifest_path=path, why="empty",
         manifest_text=summary_text(manifest_text().replace(CID, empty).replace("title: Work", "title: Empty")))
    until(lambda: store_rows(root, "SELECT COUNT(*) FROM projection_state WHERE state!='current'") == [(0,)])
    view = root / path
    view.write_bytes(view.read_bytes().replace(b"view_mode: summary\n", b""))
    until(lambda: store_rows(root, "SELECT COUNT(*) FROM held_candidates WHERE collection_id=?", (empty,)) == [(1,)])
    assert store_rows(root, "SELECT view_mode FROM collections WHERE collection_id=?", (empty,)) == [("summary",)]
    assert store_rows(root, "SELECT COUNT(*) FROM txns WHERE operation='view_edit'") == [(0,)]


def test_edit_back_waits_for_its_release_on_a_migrated_collection(served, monkeypatch):
    """Defect: edit-back applies an owner's item edit under a release that serves migrated
    collections but keeps edit-back off."""
    import time

    from exomem.collection_store import runtime

    root = served.root
    _, path = published_view(root, "Waiting", count=1)
    monkeypatch.setattr(capability, "RELEASED", capability.RELEASED - {VIEWS})
    path.write_bytes(path.read_bytes().replace(b"count: 1", b"count: 5"))
    for _ in range(6):  # past the settle window; each served request wakes the views tick
        runtime._server(root).call(lambda: None)
        time.sleep(0.5)
    assert counted(root, "Waiting") == 1
    assert store_rows(root, "SELECT COUNT(*) FROM held_candidates") == [(0,)]
    monkeypatch.setattr(capability, "RELEASED", capability.RELEASED | {VIEWS})
    until(lambda: counted(root, "Waiting") == 5)


def test_an_edited_summary_page_is_held_by_the_served_publication_pass(store):
    """Defect: the served publication pass leaves an edited summary page to edit-back, which reads
    only item and manifest views, so the page is never held or rendered again."""
    from test_collection_store_summary import bulk, pages
    from test_collection_store_summary import create as create_summary

    from exomem.governance.principal import owner_principal, request_scope

    with request_scope(owner_principal()):
        create_summary(store)
        bulk(store, [{"title": "One", "count": 1}])
        store.reconcile_views()
        store.reconcile_views()
        page = store.root / pages(store)[0]
        edited = page.read_bytes() + b"\n- title: Two\n  count: 2\n"
        page.write_bytes(edited)
        bulk(store, [{"title": "Three", "count": 3}])
        for _ in range(6):
            store.reconcile_views(pending_only=True)
    assert [(code, raw) for _, code, raw in holds(store)] == [("SUMMARY_VIEW_READ_ONLY", edited)]
    assert page.read_bytes() != edited


def test_reordering_a_manifest_views_properties_commits_nothing(store):
    """Defect: a formatting-only manifest edit commits a revise, which also refuses an in-flight
    revise that carried the earlier manifest hash."""
    from exomem import vault

    create(store)
    store.reconcile_views()
    path = store.root / manifest_path()
    rendered = path.read_bytes()
    frontmatter, body, _ = vault.parse_frontmatter(rendered.decode(), strict=True)
    reordered = {name: frontmatter[name] for name in reversed(list(frontmatter))}
    edited = ("---\n" + vault.serialize_frontmatter(reordered) + "\n---\n" + body).encode()
    assert edited != rendered
    path.write_bytes(edited)
    before = txns(store)
    passes(store, 0.0, SETTLED)
    assert txns(store) == before and holds(store) == [] and path.read_bytes() == rendered


def test_a_view_correction_held_under_an_earlier_id_shape_can_be_discarded(store):
    """Defect: a correction an earlier release held under a 24-hex id is listed for attention but
    every discard answers HELD_NOT_FOUND."""
    _, path = appended(store, count=1)
    path.write_bytes(path.read_bytes().replace(b"count: 1", b"count: broken"))
    store.reconcile_views()
    [(held, _, _)] = holds(store)
    legacy = hashlib.sha256(held.encode()).hexdigest()[:24]
    store.connection.execute("UPDATE held_candidates SET held_id=? WHERE held_id=?", (legacy, held))
    store.connection.commit()
    store.discard_held(CID, held=legacy, why="an older hold")
    assert holds(store) == []


# Log views: one view of every row, edited block by block (design §6, §16 A2).


def test_a_mixed_log_save_commits_prose_and_valid_rows_and_preserves_other_blocks(store):
    """A partial row commit must not authorize publication over unapplied prose or invalid blocks."""
    from test_collection_store_writer import OTHER, log_manifest_text

    store.create_collection(manifest_path(), log_manifest_text(), why="create")
    store.append_record(CID, item={"title": "One", "details": []}, item_key=KEY, why="capture")
    store.append_record(CID, item={"title": "Two", "details": []}, item_key=OTHER, why="capture")
    receipt = store.append_record(CID, item={"title": "Extra", "details": [{"field": "reps", "value": "3"}]},
                                  item_key="33333333-3333-4333-8333-333333333333", why="capture")
    store.reconcile_views()
    path = store.root / receipt["affected_paths"][0]
    edited = b"Authored prose.\n" + path.read_bytes().replace(b"### One", b"### First").replace(b"### Two", b"### Second")
    edited = edited.replace(b"- reps | 3", b"- reps | 3\nUnparsed prose inside a block.")
    template = b"### Template\n- reps | 8\n\n"
    path.write_bytes(edited + template)
    passes(store, 0.0, SETTLED, HELD + SETTLED)
    assert len(txns(store, "view_frame_edit")) == 1 and len(txns(store, "view_edit")) == 2
    assert path.read_bytes().startswith(b"Authored prose.\n") and b"### First" in path.read_bytes()
    assert sorted(code for _, code, _ in holds(store)) == ["VIEW_INVALID", "VIEW_UNBOUND"]
    assert next(raw for _, code, raw in holds(store) if code == "VIEW_UNBOUND") == template
    assert b"Unparsed prose inside a block." in next(raw for _, code, raw in holds(store) if code == "VIEW_INVALID")
    assert store.connection.execute("SELECT profile_operation FROM txns WHERE operation='view_frame_edit'").fetchone() == (None,)
    assert store.connection.execute("SELECT COUNT(*) FROM item_versions").fetchone() == (5,)


def test_a_stop_between_log_component_commits_leaves_the_complete_buffer_for_replay(store):
    """A committed frame and first row must not displace a second row edit after interruption."""
    from test_collection_store_writer import OTHER, log_manifest_text

    store.create_collection(manifest_path(), log_manifest_text(), why="create")
    store.append_record(CID, item={"title": "One", "details": []}, item_key=KEY, why="capture")
    receipt = store.append_record(CID, item={"title": "Two", "details": []}, item_key=OTHER, why="capture")
    store.reconcile_views()
    path = store.root / receipt["affected_paths"][0]
    raw = b"New prose.\n" + path.read_bytes().replace(b"### One", b"### First").replace(b"### Two", b"### Second")
    path.write_bytes(raw)
    cursor = store.connection.execute("SELECT * FROM projection_state WHERE kind='log'")
    projection = dict(zip([column[0] for column in cursor.description], cursor.fetchone(), strict=True))
    observation = edit_back.Observation(projection["path"], projection, hashlib.sha256(raw).hexdigest(), None, raw)
    with store.read_snapshot():
        found = edit_back.classify(store, projection, raw)
    store.apply_view_frame(observation, found.frame)
    store.apply_view_edit(observation, found.blocks[0])
    store.reconcile_views(paths={observation.path: observation})
    assert path.read_bytes() == raw  # a restart sees every unapplied component
    passes(store, 0.0, SETTLED)
    assert len(txns(store, "view_frame_edit")) == 1 and len(txns(store, "view_edit")) == 2
    assert holds(store) == [] and b"### First" in path.read_bytes() and b"### Second" in path.read_bytes()


def test_a_stale_frame_cannot_replace_new_prose_even_when_row_stamps_are_current(store):
    """Row versions alone cannot detect two competing edits of surrounding prose."""
    from test_collection_store_writer import log_manifest_text

    store.create_collection(manifest_path(), log_manifest_text(), why="create")
    receipt = store.append_record(CID, item={"title": "One", "details": []}, item_key=KEY, why="capture")
    store.reconcile_views()
    path = store.root / receipt["affected_paths"][0]
    stale = path.read_bytes()
    path.write_bytes(b"First prose.\n" + stale)
    passes(store, 0.0, SETTLED)
    edited = b"Stale prose.\n" + stale
    path.write_bytes(edited)
    passes(store, 60.0, 60.0 + HELD)
    assert len(txns(store, "view_frame_edit")) == 1
    assert [(code, raw) for _, code, raw in holds(store)] == [("VIEW_INVALID", edited)]
    assert path.read_bytes().startswith(b"First prose.\n")


def test_a_row_commit_does_not_stale_an_unchanged_frame_base(store):
    """Saving old unchanged row blocks with new prose must keep a newer row commit."""
    from test_collection_store_writer import log_manifest_text

    store.create_collection(manifest_path(), log_manifest_text(), why="create")
    receipt = store.append_record(CID, item={"title": "One", "details": []}, item_key=KEY, why="capture")
    store.reconcile_views()
    path = store.root / receipt["affected_paths"][0]
    stale = path.read_bytes()
    for title in ("Agent first", "Agent second", "Agent"):
        receipt = store.update_record(CID, item_key=KEY, changes={"title": title}, why="agent",
                                      expected_container_hash=receipt["after_container_hash"],
                                      expected_item_version=receipt["after_item_hash"])
        store.reconcile_views()
    path.write_bytes(b"New prose.\n" + stale)
    passes(store, 0.0, SETTLED)
    assert len(txns(store, "view_frame_edit")) == 1 and txns(store, "view_edit") == [] and holds(store) == []
    assert path.read_bytes().startswith(b"New prose.\n") and b"### Agent" in path.read_bytes()


def test_a_frame_returning_to_its_base_accepts_an_unapplied_edit_but_replays_an_applied_save(store):
    """Frame content CAS survives A→B→A while an earlier committed save remains one transition."""
    from test_collection_store_writer import log_manifest_text

    store.create_collection(manifest_path(), log_manifest_text(), why="create")
    receipt = store.append_record(CID, item={"title": "One", "details": []}, item_key=KEY, why="capture")
    store.reconcile_views()
    path = store.root / receipt["affected_paths"][0]
    base = path.read_bytes()
    applied = b"First prose.\n" + base
    path.write_bytes(applied)
    passes(store, 0.0, SETTLED)
    path.write_bytes(path.read_bytes().removeprefix(b"First prose.\n"))
    passes(store, 60.0, 60.0 + SETTLED)
    path.write_bytes(b"Unapplied prose.\n" + base)
    passes(store, 120.0, 120.0 + SETTLED)
    assert len(txns(store, "view_frame_edit")) == 3 and path.read_bytes().startswith(b"Unapplied prose.\n")
    path.write_bytes(applied)
    passes(store, 180.0, 180.0 + SETTLED)
    assert len(txns(store, "view_frame_edit")) == 3 and path.read_bytes().startswith(b"Unapplied prose.\n")
    assert holds(store) == [] and txns(store, "view_edit") == []


def test_an_empty_eof_log_keeps_authored_prose_and_a_template_around_its_invisible_stamp(store, tmp_path):
    """An invisible marker must not add a newline or turn below-heading template insertion into a whole hold."""
    from test_collection_store_views import SESSION, X3_ID, imported_log

    from exomem import record_formats
    from exomem.collection_store import export, log_view

    def empty_eof(data):
        section = next(heading for heading in record_formats._headings_outside_fences(data) if heading.level == 2)
        return data[:section.end].removesuffix(b"\n")

    path = imported_log(store, tmp_path, empty_eof)
    row, manifest, _ = store._collection(X3_ID)
    # Materialize the imported empty projection through its ordinary pending-render seam.
    with store._mutation(reconcile=True):
        store._pending_log(manifest, row["generation"])
    store.reconcile_views()
    raw = path.read_bytes()
    heading = record_formats._headings_outside_fences(raw)[-1]
    assert not raw.endswith(b"\n") and b"<!-- exomem-frame " in raw[:heading.start]
    # Prose typed between the invisible stamp and heading remains authored prefix text.
    path.write_bytes(raw[:heading.start] + b"New prose.\n" + raw[heading.start:])
    passes(store, 0.0, SETTLED)
    assert len(txns(store, "view_frame_edit")) == 1 and not path.read_bytes().endswith(b"\n")
    # An actual owner-added terminal newline is a frame edit, even while the log is empty.
    path.write_bytes(path.read_bytes() + b"\n")
    passes(store, 60.0, 60.0 + SETTLED)
    assert len(txns(store, "view_frame_edit")) == 2 and path.read_bytes().endswith(b"\n")
    template = log_view.render_block(manifest, {"item_key": KEY, "values": SESSION}, "\n").encode()
    template = b"".join(line for line in template.splitlines(keepends=True)
                        if record_formats._MARKER.fullmatch(line.strip()) is None)
    path.write_bytes(path.read_bytes() + template)
    passes(store, 120.0, 120.0 + SETTLED)
    [(reference, code, held)] = holds(store)
    assert code == "VIEW_UNBOUND" and held == template and b"New prose.\n" in path.read_bytes()
    receipt = store.append_record(X3_ID, held=reference, why="adopt template")
    store.reconcile_views()
    frame_count = len(txns(store, "view_frame_edit"))
    path.write_bytes(path.read_bytes().replace(b"Deadlift | grey | 22", b"Deadlift | grey | 23"))
    passes(store, 180.0, 180.0 + SETTLED)
    assert len(txns(store, "view_frame_edit")) == frame_count and len(txns(store, "view_edit")) == 1
    frame = store.connection.execute("SELECT log_frame_json FROM collections WHERE collection_id=?", (X3_ID,)).fetchone()[0]
    [(_, exported)] = export._render_log(store.root, store.connection, manifest, X3_ID, frame)
    assert b"New prose.\n" in exported and b"Deadlift | grey | 23" in exported
    assert receipt["outcome"] == "committed" and holds(store) == []


@pytest.mark.parametrize("problem", ["duplicate", "foreign", "future"])
def test_a_bad_frame_stamp_prevents_every_component_of_a_mixed_save(store, problem):
    """Position flexibility must not let duplicate or foreign frame proof accompany applied rows."""
    from test_collection_store_writer import log_manifest_text

    store.create_collection(manifest_path(), log_manifest_text(), why="create")
    receipt = store.append_record(CID, item={"title": "One", "details": []}, item_key=KEY, why="capture")
    store.reconcile_views()
    path = store.root / receipt["affected_paths"][0]
    raw = b"New prose.\n" + path.read_bytes().replace(b"### One", b"### Edited")
    marker = next(line for line in raw.splitlines(keepends=True) if line.startswith(b"<!-- exomem-frame "))
    if problem == "duplicate":
        raw += marker
    else:
        stamp = json.loads(marker[len(b"<!-- exomem-frame "):-len(b" -->\n")])
        if problem == "foreign":
            stamp["s"] = "99999999-9999-4999-8999-999999999999"
        else:
            stamp["v"] = store.connection.execute("SELECT generation FROM collections").fetchone()[0] + 1
        raw = raw.replace(marker, b"<!-- exomem-frame " + json.dumps(stamp).encode() + b" -->\n")
    path.write_bytes(raw)
    passes(store, 0.0, HELD)
    assert txns(store, "view_frame_edit") == txns(store, "view_edit") == []
    assert [(code, held) for _, code, held in holds(store)] == [(
        "VIEW_FOREIGN" if problem == "foreign" else "VIEW_INVALID", raw)]


def test_missing_historical_frame_context_is_not_reconstructed_from_the_current_manifest(store):
    """Governance backfill can hold bytes but cannot invent proof that edited prose was rendered."""
    from test_collection_store_writer import log_manifest_text

    store.create_collection(manifest_path(), log_manifest_text(), why="create")
    receipt = store.append_record(CID, item={"title": "One", "details": []}, item_key=KEY, why="capture")
    store.reconcile_views()
    path = store.root / receipt["affected_paths"][0]
    descriptor = json.loads(store.connection.execute("SELECT install_json FROM projection_state WHERE kind='log'").fetchone()[0])
    for context in [descriptor.get("render_context"), *descriptor.get("source_contexts", [])]:
        if context:
            context.pop("frame_hash", None)
    store.connection.execute("UPDATE projection_state SET install_json=? WHERE kind='log'", (json.dumps(descriptor),))
    store.connection.commit()
    raw = b"Unproved prose.\n" + path.read_bytes()
    path.write_bytes(raw)
    passes(store, 0.0, HELD)
    assert txns(store, "view_frame_edit") == []
    assert [(code, held) for _, code, held in holds(store)] == [("VIEW_INVALID", raw)]


def test_a_held_log_conflict_resumes_only_the_displayed_edit_and_a_template_appends_once(store):
    """Log resume must parse a block, preserve newer hidden fields and discard inferred template identity."""
    from test_collection_store_writer import log_manifest_text

    text = log_manifest_text().replace("    title: {type: string, required: true}",
                                       "    title: {type: string, required: true}\n    note: {type: string}")
    store.create_collection(manifest_path(), text, why="create")
    receipt = store.append_record(CID, item={"title": "One", "note": "old", "details": []}, item_key=KEY, why="capture")
    store.reconcile_views()
    path = store.root / receipt["affected_paths"][0]
    stale = path.read_bytes()
    current = store.update_record(CID, item_key=KEY, changes={"note": "new"}, why="agent",
                        expected_container_hash=receipt["after_container_hash"],
                        expected_item_version=receipt["after_item_hash"])
    store.reconcile_views()
    path.write_bytes(stale.replace(b"### One", b"### Edited") + b"### Template\n- reps | 8\n\n")
    passes(store, 0.0, SETTLED)
    held = {code: reference for reference, code, _ in holds(store)}
    store.update_record(CID, item_key=KEY, held=held["VIEW_CONFLICT"], why="resume",
                        expected_container_hash=current["after_container_hash"],
                        expected_item_version=current["after_item_hash"])
    values = json.loads(store.connection.execute("SELECT values_json FROM items WHERE item_key=?", (KEY,)).fetchone()[0])
    assert values == {"title": "Edited", "note": "new", "details": []}
    first = store.append_record(CID, held=held["VIEW_UNBOUND"], why="adopt template", request_id="template-resume")
    replay = store.append_record(CID, held=held["VIEW_UNBOUND"], why="adopt template", request_id="template-resume")
    assert first["audit_correlation"] == replay["audit_correlation"]
    assert store.connection.execute("SELECT COUNT(*) FROM items").fetchone() == (2,)


def test_an_edit_of_a_stale_log_block_is_held_as_a_conflict(store):
    """Defect: a log block saved from a buffer older than its row reverts the agent's newer row, or
    the owner's bytes for that block are lost when the log is rendered again."""
    from test_collection_store_writer import log_manifest_text

    store.create_collection(manifest_path(), log_manifest_text(), why="create")
    receipt = store.append_record(CID, item={"title": "One", "details": [{"field": "reps", "value": "10"}]},
                                  item_key=KEY, why="capture")
    store.reconcile_views()
    path = store.root / receipt["affected_paths"][0]
    stale = path.read_bytes()
    store.update_record(CID, item_key=KEY, changes={"details": [{"field": "reps", "value": "11"}]}, why="agent",
                        expected_container_hash=receipt["after_container_hash"],
                        expected_item_version=receipt["after_item_hash"])
    store.reconcile_views()
    edited = stale.replace(b"- reps | 10", b"- reps | 12")
    path.write_bytes(edited)
    passes(store, 0.0, SETTLED)
    [(_, code, raw)] = holds(store)
    assert code == "VIEW_CONFLICT" and b"- reps | 12" in raw
    assert json.loads(store.connection.execute("SELECT values_json FROM items").fetchone()[0])["details"] == [
        {"field": "reps", "value": "11"}]
    assert txns(store, "view_edit") == [] and b"- reps | 11" in path.read_bytes()


def test_an_imported_log_edited_before_its_first_render_is_held_and_says_how_to_resume(store, tmp_path):
    """Defect: an edit of an imported log that the store never rendered, so it carries no stamps, is
    read as an edit of the current rows, or the first render drops it without a word."""
    from test_collection_store_views import X3_ID, imported_log

    source = imported_log(store, tmp_path)
    edited = source.read_bytes().replace(b"Overhead Press | blue short | 12", b"Overhead Press | blue short | 13")
    source.write_bytes(edited)
    passes(store, 0.0, SETTLED, HELD + SETTLED)
    [(_, code, raw)] = holds(store)
    assert (code, raw) == ("VIEW_INVALID", edited)
    [diagnostic] = json.loads(store.connection.execute("SELECT diagnostics_json FROM held_candidates").fetchone()[0])
    assert [issue["code"] for issue in diagnostic["issues"]] == ["VIEW_UNSTAMPED"]
    assert diagnostic["issues"][0]["remedy"]
    assert txns(store, "view_edit") == []
    assert b"exomem-view" in source.read_bytes()
    assert store.connection.execute("SELECT COUNT(*) FROM items WHERE collection_id=?", (X3_ID,)).fetchone()[0] > 0


@pytest.mark.parametrize("stamped", ["foreign", "tampered"])
def test_a_log_blocks_foreign_or_tampered_stamp_is_held(store, stamped):
    """Defect: a log block copied from another store's view, or one whose stamp names another
    payload, is applied as the owner's edit of the current row."""
    from test_collection_store_writer import log_manifest_text

    from exomem.collection_store import connection

    store.create_collection(manifest_path(), log_manifest_text(), why="create")
    receipt = store.append_record(CID, item={"title": "One", "details": [{"field": "reps", "value": "10"}]},
                                  item_key=KEY, why="capture")
    store.reconcile_views()
    path = store.root / receipt["affected_paths"][0]
    rendered = path.read_bytes()
    instance = store.connection.execute("SELECT value FROM store_meta WHERE key='instance_id'").fetchone()[0]
    payload = store.connection.execute("SELECT payload_hash FROM items").fetchone()[0]
    tampered = (f"i={instance}", "i=00000000-0000-4000-8000-000000000001") if stamped == "foreign" \
        else (f"h={payload[:12]}", "h=000000000000")
    edited = rendered.replace(b"- reps | 10", b"- reps | 12").replace(*(part.encode() for part in tampered))
    path.write_bytes(edited)
    passes(store, 0.0, SETTLED, HELD + SETTLED)
    [(_, code, raw)] = holds(store)
    assert txns(store, "view_edit") == []
    if stamped == "foreign":
        assert (code, raw) == ("VIEW_FOREIGN", edited)
        with pytest.raises(connection.CollectionStoreError, match="COLLECTION_STORE_DIVERGED"):
            store.append_record(CID, item={"title": "Two", "details": []}, why="capture")
    else:
        assert code == "VIEW_INVALID" and b"- reps | 12" in raw and b"h=000000000000" in raw
        assert b"- reps | 10" in path.read_bytes()


def held_issues(store):
    return [issue["code"] for diagnostic in json.loads(
        store.connection.execute("SELECT diagnostics_json FROM held_candidates").fetchone()[0])
        for issue in diagnostic.get("issues", ())]


@pytest.mark.parametrize(("problem", "issue"), [
    ("duplicate_identity", "DUPLICATE_RECORD_ID"),
    ("mixed_unstamped", "VIEW_UNSTAMPED"),
    ("natural_key_conflict", "RECORD_NATURAL_KEY_CONFLICT"),
])
def test_a_refused_log_save_preserves_records_and_the_complete_rejected_input(store, problem, issue):
    """An invalid whole log or block must preserve its bytes without changing either stored row."""
    from test_collection_store_writer import OTHER, log_manifest_text

    from exomem import record_formats
    from exomem.collection_store import log_view

    store.create_collection(manifest_path(), log_manifest_text(), why="create")
    store.append_record(CID, item={"title": "One", "details": []}, item_key=KEY, why="capture")
    receipt = store.append_record(CID, item={"title": "Two", "details": []}, item_key=OTHER, why="capture")
    store.reconcile_views()
    path = store.root / receipt["affected_paths"][0]
    rendered = path.read_bytes()
    _, manifest, _ = store._collection(CID)
    authored, _ = log_view.split_frame_stamp(manifest, rendered)
    adapter = record_formats.MarkdownLogAdapter(store.root, manifest)
    parsed = adapter.read_bytes(authored)
    assert not parsed.diagnostics and len(parsed.records) == 2
    record = next(record for record in parsed.records if record.identity.key == KEY)
    block = authored[record.span.start:record.span.end]
    if problem == "duplicate_identity":
        edited = expected = rendered + block
    else:
        changed = block
        if problem == "mixed_unstamped":
            stamp = record_formats.read_log_block_stamp(block)
            assert stamp is not None
            marker = record_formats._log_block_stamp_line(stamp).encode()
            changed = changed.replace(marker + b"\n", b"")
        expected = changed.replace(b"### One", b"### Two" if problem == "natural_key_conflict" else b"### Edited")
        edited = rendered.replace(block, expected, 1)
    assert edited != rendered
    proposed, _ = log_view.split_frame_stamp(manifest, edited)
    parsed_edit = adapter.read_bytes(proposed)
    if problem == "duplicate_identity":
        assert [diagnostic.code for diagnostic in parsed_edit.diagnostics] == [issue]
    else:
        assert not parsed_edit.diagnostics and len(parsed_edit.records) == 2
    rows = store.connection.execute("SELECT * FROM items ORDER BY item_key").fetchall()
    versions = store.connection.execute("SELECT * FROM item_versions ORDER BY row_id,row_version").fetchall()
    history = txns(store)
    path.write_bytes(edited)
    passes(store, 0.0, SETTLED, HELD + SETTLED)
    [(_, code, raw)] = holds(store)
    assert (code, raw) == ("VIEW_INVALID", expected)
    assert held_issues(store) == [issue]
    assert store.connection.execute("SELECT * FROM items ORDER BY item_key").fetchall() == rows
    assert store.connection.execute("SELECT * FROM item_versions ORDER BY row_id,row_version").fetchall() == versions
    assert txns(store) == history and path.read_bytes() == rendered


def test_a_conflict_copy_keeps_its_bytes_without_moving_the_canonical_record(store):
    """A copied stamp must not make a conflict file a move or an edit of the stored record."""
    _, original = appended(store, count=1)
    raw = original.read_bytes()
    copied = original.with_name("One.sync-conflict-20261019-000000-node.md")
    copied.write_bytes(raw)
    rows = store.connection.execute("SELECT * FROM items").fetchall()
    history = txns(store)
    passes(store, 0.0, SETTLED, HELD + SETTLED)
    assert [(code, held) for _, code, held in holds(store)] == [("VIEW_CONFLICT_COPY", raw)]
    assert store.connection.execute("SELECT * FROM items").fetchall() == rows
    assert txns(store) == history
    assert original.read_bytes() == copied.read_bytes() == raw


def test_a_log_block_edit_that_also_adds_text_is_held_with_the_text(store):
    """Defect: a block edit that changes a value and adds a line of prose applies the value and
    drops the prose when the log is rendered again."""
    from test_collection_store_writer import log_manifest_text

    store.create_collection(manifest_path(), log_manifest_text(), why="create")
    receipt = store.append_record(CID, item={"title": "One", "details": [{"field": "reps", "value": "10"}]},
                                  item_key=KEY, why="capture")
    store.reconcile_views()
    path = store.root / receipt["affected_paths"][0]
    path.write_bytes(path.read_bytes().replace(b"- reps | 10\n", b"- reps | 12\nFelt strong today.\n"))
    passes(store, 0.0, SETTLED, HELD + SETTLED)
    assert txns(store, "view_edit") == []
    [(_, code, raw)] = holds(store)
    assert code == "VIEW_INVALID" and b"- reps | 12\nFelt strong today.\n" in raw
    assert held_issues(store) == ["VIEW_BLOCK_TEXT"]
    assert json.loads(store.connection.execute("SELECT values_json FROM items").fetchone()[0])["details"] == [
        {"field": "reps", "value": "10"}]


def test_a_spacing_only_edit_of_a_log_block_is_rendered_again_without_a_hold(store):
    """Defect: re-spacing a child row of a log view, which parses to the same values, is held as
    text the store cannot keep, or commits an update."""
    from test_collection_store_writer import log_manifest_text

    store.create_collection(manifest_path(), log_manifest_text(), why="create")
    receipt = store.append_record(CID, item={"title": "One", "details": [{"field": "reps", "value": "10"}]},
                                  item_key=KEY, why="capture")
    store.reconcile_views()
    path = store.root / receipt["affected_paths"][0]
    rendered = path.read_bytes()
    path.write_bytes(rendered.replace(b"- reps | 10\n", b"  - reps|10\n"))
    before = txns(store)
    passes(store, 0.0, SETTLED, HELD + SETTLED)
    assert holds(store) == [] and txns(store) == before and path.read_bytes() == rendered


def test_a_formatting_only_log_save_preserves_spaces_in_stored_values(store):
    """A grammar-trimmed heading must not turn a formatting save into a correction."""
    from test_collection_store_writer import log_manifest_text

    store.create_collection(manifest_path(), log_manifest_text(), why="create")
    values = {"title": "  One  ", "details": [{"field": "reps", "value": " 10 "}]}
    receipt = store.append_record(CID, item=values, item_key=KEY, why="capture")
    store.reconcile_views()
    path = store.root / receipt["affected_paths"][0]
    rendered = path.read_bytes()
    edited = rendered.replace(b"###   One  \n", b"### One\n")
    assert edited != rendered
    path.write_bytes(edited)
    before = txns(store)
    passes(store, 0.0, SETTLED, HELD + SETTLED)
    assert holds(store) == [] and txns(store) == before
    assert json.loads(store.connection.execute("SELECT values_json FROM items").fetchone()[0]) == values
    assert path.read_bytes() == rendered


def test_a_log_field_edit_preserves_spaces_in_an_untouched_field(store):
    """Editing a child row must not trim an API-written heading in the same block."""
    from test_collection_store_writer import log_manifest_text

    store.create_collection(manifest_path(), log_manifest_text(), why="create")
    receipt = store.append_record(CID, item={"title": "  One  ", "details": [{"field": "reps", "value": "10"}]},
                                  item_key=KEY, why="capture")
    store.reconcile_views()
    path = store.root / receipt["affected_paths"][0]
    rendered = path.read_bytes()
    edited = rendered.replace(b"- reps | 10\n", b"- reps | 11\n")
    assert edited != rendered
    path.write_bytes(edited)
    passes(store, 0.0, SETTLED)
    version, raw = store.connection.execute("SELECT row_version,values_json FROM items").fetchone()
    assert version == 2 and holds(store) == []
    assert json.loads(raw) == {"title": "  One  ", "details": [{"field": "reps", "value": "11"}]}
    assert len(txns(store, "view_edit")) == 1


def test_a_stale_log_resume_preserves_untouched_padding_and_a_newer_hidden_field(store):
    """A held child edit must not trim its old heading or undo a newer API-only field."""
    from test_collection_store_writer import log_manifest_text

    text = log_manifest_text().replace("    title: {type: string, required: true}",
                                       "    title: {type: string, required: true}\n    note: {type: string}")
    store.create_collection(manifest_path(), text, why="create")
    receipt = store.append_record(CID, item={"title": "  One  ", "note": "old",
                                  "details": [{"field": "reps", "value": "10"}]}, item_key=KEY, why="capture")
    store.reconcile_views()
    path = store.root / receipt["affected_paths"][0]
    stale = path.read_bytes()
    current = store.update_record(CID, item_key=KEY, changes={"note": "new"}, why="agent",
                                  expected_container_hash=receipt["after_container_hash"],
                                  expected_item_version=receipt["after_item_hash"])
    store.reconcile_views()
    edited = stale.replace(b"- reps | 10\n", b"- reps | 11\n")
    assert edited != stale
    path.write_bytes(edited)
    passes(store, 0.0, SETTLED)
    [(held, code, _)] = holds(store)
    assert code == "VIEW_CONFLICT"
    store.update_record(CID, item_key=KEY, held=held, why="resume",
                        expected_container_hash=current["after_container_hash"],
                        expected_item_version=current["after_item_hash"])
    values = json.loads(store.connection.execute("SELECT values_json FROM items").fetchone()[0])
    assert values == {"title": "  One  ", "note": "new", "details": [{"field": "reps", "value": "11"}]}


def test_a_log_block_that_names_no_row_is_held_and_appends_nothing(store):
    """Defect: a block the owner typed into the log, which no record id binds to a row, is dropped
    by the next render, or becomes a row without a governed append."""
    from test_collection_store_writer import log_manifest_text

    store.create_collection(manifest_path(), log_manifest_text(), why="create")
    receipt = store.append_record(CID, item={"title": "One", "details": []}, item_key=KEY, why="capture")
    store.reconcile_views()
    path = store.root / receipt["affected_paths"][0]
    typed = b"### Two\n- reps | 5\n\n"
    path.write_bytes(path.read_bytes().replace(b"## Entries\n", b"## Entries\n" + typed, 1))
    before = txns(store)
    passes(store, 0.0, SETTLED)
    assert [(code, raw) for _, code, raw in holds(store)] == [("VIEW_UNBOUND", typed)]
    assert txns(store) == before and b"### Two" not in path.read_bytes()


def test_a_held_whole_log_view_is_withheld_from_a_caller_who_cannot_read_every_row(store):
    """Defect: a hold of the whole log view, whose bytes show every row, is released to a caller
    from whom one of those rows is withheld."""
    from test_collection_store_writer import OTHER, log_manifest_text
    from test_governance_egress import _external, write_rule, write_scope

    from exomem.collection_store import authority
    from exomem.collection_store.preview import preview_store
    from exomem.governance.principal import owner_principal, request_scope

    store.create_collection(manifest_path(), log_manifest_text(), why="create")
    store.append_record(CID, item={"title": "Open", "details": []}, item_key=KEY, why="capture")
    receipt = store.append_record(CID, item={"title": "Secret", "details": []}, item_key=OTHER, why="capture")
    store.reconcile_views()
    log = receipt["affected_paths"][0]
    path = store.root / log
    from exomem.collection_store import log_view

    _, manifest, _ = store._collection(CID)
    unstamped, _ = log_view.split_frame_stamp(manifest, path.read_bytes())
    path.write_bytes(b"A note above the log.\n" + unstamped)  # Missing frame proof holds the whole buffer.
    passes(store, 0.0, SETTLED, HELD + SETTLED)
    assert [code for _, code, _ in holds(store)] == ["VIEW_INVALID"] and held_issues(store) == ["VIEW_FRAME_CHANGED"]
    sid = store.connection.execute("SELECT value FROM store_meta WHERE key='store_id'").fetchone()[0]
    marker = authority.marker_path(store.root)
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({
        "version": 2, "mode": "store", "default_authority": "file", "store_id": sid, "authority_epoch": 1,
        "collections": [{"collection_id": CID, "manifest_path": manifest_path(), "authority": "store",
                         "store_id": sid, "semantic_profile": "records", "source_path": log,
                         "layout": "markdown-log"}],
        "collection_store_fence": {"capability": "collections-store-v1", "generation": 1},
    }))
    write_scope(store.root, paths="Unrelated/**")
    scope = store.root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + f"refs: [exomem://record/{CID}/{OTHER}]\n")  # withholds one row by its reference
    write_rule(store.root, ceiling=0)

    [held] = store.connection.execute("SELECT held_id FROM held_candidates").fetchone()

    def attention(who):
        with preview_store(store.root, store.handle), request_scope(who):
            findings, unknown = edit_back.attention_findings(store.root)
        return [finding.meta["held_id"] for finding in findings], unknown

    assert attention(owner_principal()) == ([held], None)
    assert attention(_external()) == ([], None)


X3 = "Knowledge Base/Records/Health/X3/_collection.md"
X3_LOG = "Knowledge Base/Records/Health/X3/Training Log.md"


@pytest.fixture
def served_log(env, capsys, monkeypatch):
    """A migrated vault whose X3 log has a BOM, CRLF lines and a section after its own, served."""
    from record_fixtures import copy_x3_fixture
    from test_collection_store_migration import migrate, stop_writer

    from exomem.init import init_vault

    monkeypatch.setattr(edit_back, "FULL_PASS_SECONDS", 0.3)
    root = env
    init_vault(root)
    copy_x3_fixture(root)
    log = root / X3_LOG
    log.write_bytes(b"\xef\xbb\xbf" + log.read_bytes().replace(b"\n", b"\r\n"))
    stop_writer()
    assert migrate(root, "apply") == 0
    monkeypatch.setattr(capability, "RELEASED", capability.RELEASED | {VIEWS})
    capsys.readouterr()
    vault = Served(root)
    vault.start()
    try:
        yield vault
    finally:
        if vault.service is not None:
            vault.stop()


def log_state(root):
    return store_rows(root, "SELECT state FROM projection_state WHERE path=?", (X3_LOG,))


def test_a_served_log_view_is_exports_render_and_takes_one_edited_block_as_one_update(served_log):
    """Defect: the projector and the export render a log by two rules that disagree, so the view
    the owner edits is not the file an export writes back; or a saved log view is applied as
    anything but one governed update per edited block, or a block the owner removed is dropped."""
    from contextlib import closing

    from test_collection_store_views import SESSION, SESSION_HEADING

    from exomem import record_formats
    from exomem.collection_store import connection, export, log_view

    root = served_log.root
    owner(root, "record_memory", action="append", collection=X3, item=SESSION, why="a store-mode session")
    until(lambda: log_state(root) == [("current",)])
    view = (root / X3_LOG).read_bytes()
    with closing(connection.open_reader(connection.store_path(root))) as conn:
        text, frame = conn.execute(
            "SELECT m.manifest_text,c.log_frame_json FROM collections c JOIN collection_manifests m "
            "ON m.collection_id=c.collection_id AND m.manifest_version=c.manifest_version").fetchone()
        manifest = collections.parse_manifest_bytes(root, X3, text.encode())
        [(_, exported)] = export._render_log(root, conn, manifest, manifest.collection_id, frame)
    # The export writes no stamps, because file mode has no store to name.
    authored, _ = log_view.split_frame_stamp(manifest, view)
    unstamped = b"".join(line for line in authored.splitlines(keepends=True)
                         if record_formats.read_log_block_stamp(line) is None)
    assert unstamped == exported and view.startswith(b"\xef\xbb\xbf") and SESSION_HEADING in view
    blocks = view.split(b"### ")
    removed = next(block for block in blocks if block.startswith("2024-10-13 · Pull".encode()))
    edited = view.replace(b"Overhead Press | blue short | 12", b"Overhead Press | blue short | 13")
    (root / X3_LOG).write_bytes(edited.replace(b"### " + removed, b""))
    until(lambda: store_rows(root, "SELECT code FROM held_candidates") == [("VIEW_DELETED",)])
    assert store_rows(root, "SELECT operation,actor FROM txns WHERE operation='view_edit'") == [
        ("view_edit", "owner:view-edit")]
    until(lambda: log_state(root) == [("current",)] and b"### " + removed in (root / X3_LOG).read_bytes())
    assert b"Overhead Press | blue short | 13" in (root / X3_LOG).read_bytes()


def test_stopping_the_service_installs_its_pending_log_view(served_log, monkeypatch):
    """Defect: a stop or a lease handoff leaves a committed append's log view pending, so the vault
    file stays behind the store until another holder starts."""
    from test_collection_store_views import SESSION, SESSION_HEADING

    from exomem.collection_store import runtime

    root = served_log.root
    monkeypatch.setattr(runtime.StoreServer, "_views_tick", lambda self: 3600.0)  # the projector stays idle
    owner(root, "record_memory", action="append", collection=X3, item=SESSION, why="a store-mode session")
    assert log_state(root) == [("pending",)]
    served_log.stop()
    assert SESSION_HEADING in (root / X3_LOG).read_bytes()
