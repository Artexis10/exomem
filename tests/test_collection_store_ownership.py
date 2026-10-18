"""Canonical ownership survives stale hints and concurrent first admission."""

import hashlib
import sqlite3
import threading
from contextlib import closing
from types import SimpleNamespace

import pytest
from test_collection_store_service import (
    CID,
    DAILY,
    DAILY_PATH,
    DAILY_TEXT,
    Service,
    _published,
    _served_vault,
    _until,
    call,
    coordinator,
    offline_create,
    store_meta,
    titles,
)
from test_collection_store_service import (
    service as service,
)
from test_collection_store_writer import KEY, manifest_path, manifest_text
from test_governance_egress import _receipt_records

from exomem import (
    get_page,
    list_directory,
    overview,
    state_migration,
    vault_portability,
    writer_lease,
)
from exomem.cli_ops import OpError
from exomem.collection_store import authority, connection, owner, replica, runtime, schema
from exomem.collection_store.preview import preview_store


@pytest.mark.parametrize("hint", ["older", "absent"])
def test_later_collection_cannot_return_to_files_after_hint_loss_and_restart(service, hint):
    root = service.root
    older = authority.read_marker(root)
    call(root, "record_memory", action="create", manifest_path=DAILY_PATH,
         manifest_text=DAILY_TEXT, why="second collection")
    call(root, "record_memory", action="append", collection=DAILY,
         item={"title": "Second canonical"}, item_key=KEY, why="capture")
    service.stop()
    # A generated declaration and row cannot appoint themselves as file authority.
    (root / DAILY_PATH).write_text(manifest_text().replace(CID, DAILY))
    decoy = root / DAILY_PATH
    decoy = decoy.parent / "Items" / (KEY + ".md")
    decoy.parent.mkdir(parents=True, exist_ok=True)
    decoy.write_text("---\ntype: record\nrecord_id: " + KEY + "\ntitle: File decoy\n---\n")
    if hint == "older":
        authority.marker_path(root).write_bytes(older)
    else:
        authority.marker_path(root).unlink()
    restarted = Service(root, writer_lease.start_server_lifecycle())
    try:
        _until(lambda: restarted.manager.status().get("collection_store", {}).get("status") == "admitted")
        assert titles(root, CID) == ["Canonical"]
        assert titles(root, DAILY) == ["Second canonical"]
        assert titles(root, DAILY_PATH) == ["Second canonical"]
        third = "77777777-7777-4777-8777-777777777777"
        path = DAILY_PATH.replace("Daily", "Third")
        text = DAILY_TEXT.replace(DAILY, third).replace("title: Daily", "title: Third")
        call(root, "record_memory", action="create", manifest_path=path, manifest_text=text, why="later collection")
        assert titles(root, DAILY) == ["Second canonical"]
        ordinary = "Knowledge Base/Notes/Movable.md"
        (root / ordinary).parent.mkdir(parents=True, exist_ok=True)
        (root / ordinary).write_text("Ordinary note.\n")
        for arguments in (
            {"operation": "delete", "path": DAILY_PATH, "confirm": True},
            {"operation": "move", "old_path": DAILY_PATH, "new_path": "Knowledge Base/Notes/Moved.md"},
            {"operation": "move", "old_path": ordinary, "new_path": str(decoy.relative_to(root))},
            {"operation": "delete", "path": str((root / DAILY_PATH).parent.relative_to(root)),
             "confirm": True, "recursive": True},
        ):
            with pytest.raises((ValueError, OpError), match="COLLECTION_VIEW_PATH"):
                call(root, "manage_memory_file", **arguments)
        assert titles(root, DAILY) == ["Second canonical"]
    finally:
        restarted.stop()


def test_first_admission_invalidates_a_whole_overview_without_holding_its_creator(tmp_path, monkeypatch):
    root = _served_vault(tmp_path, monkeypatch)
    captured, resume = threading.Event(), threading.Event()
    walk = overview.os.walk
    result = []

    def paused(*args, **kwargs):
        if threading.current_thread().name == "ownership-overview":
            captured.set()
            assert resume.wait(15)
        yield from walk(*args, **kwargs)

    monkeypatch.setattr(overview.os, "walk", paused)
    with coordinator(tmp_path / "coordinator.sqlite"):
        def read():
            try:
                result.append(overview.overview(root))
            except Exception as error:  # noqa: BLE001 - transfer the public failure to the asserting thread
                result.append(error)

        reader = threading.Thread(target=read, name="ownership-overview")
        reader.start()
        try:
            assert captured.wait(10)
            assert offline_create(root, tmp_path / "c.md", manifest_path(), why="first collection") == 0
        finally:
            resume.set()
            reader.join(15)
        assert not reader.is_alive()
        assert len(result) == 1 and isinstance(result[0], OpError), result
        assert result[0].code == "COLLECTION_STORE_UNAVAILABLE"
        assert "Canonical" not in str(result[0])


def test_first_admission_invalidates_an_owner_listing_after_its_physical_snapshot(tmp_path, monkeypatch):
    from exomem.governance.principal import owner_principal, request_scope

    root = _served_vault(tmp_path, monkeypatch)
    captured, resume = threading.Event(), threading.Event()
    original = list_directory._entry_for_held
    result = []

    def paused(*args, **kwargs):
        entry = original(*args, **kwargs)
        if threading.current_thread().name == "ownership-listing" and not captured.is_set():
            captured.set()
            assert resume.wait(15)
        return entry

    monkeypatch.setattr(list_directory, "_entry_for_held", paused)

    def read():
        with request_scope(owner_principal(surface="cli")):
            try:
                result.append(list_directory.list_directory(root, path="Knowledge Base/Records", recursive=True))
            except Exception as error:  # noqa: BLE001 - transfer the public result to the asserting thread
                result.append(error)

    with coordinator(tmp_path / "coordinator.sqlite"):
        reader = threading.Thread(target=read, name="ownership-listing")
        reader.start()
        try:
            assert captured.wait(10)
            assert offline_create(root, tmp_path / "c.md", manifest_path(), why="first collection") == 0
        finally:
            resume.set()
            reader.join(15)
    assert not reader.is_alive()
    assert len(result) == 1 and isinstance(result[0], OpError), result
    assert result[0].code == "COLLECTION_STORE_UNAVAILABLE"


@pytest.mark.parametrize(("failed_read", "alias"), [(False, False), (False, True), (True, False)])
def test_ownership_rejection_keeps_credential_evidence_without_a_successful_release(tmp_path, monkeypatch, failed_read, alias):
    root = _served_vault(tmp_path, monkeypatch)
    relative = "Knowledge Base/Records/Independent.md"
    secret = "Authorization: Bearer sk-proj-9dQm2XvKpLzR4wTnBcYeF8aHgJ1sVuNiO0rEyMdA"
    (root / relative).parent.mkdir(parents=True, exist_ok=True)
    (root / relative).write_text("# Ordinary page\n\n" + secret + "\n")
    requested = relative
    if alias:
        requested = "Knowledge Base/Notes/Alias.md"
        (root / requested).parent.mkdir(parents=True, exist_ok=True)
        (root / requested).symlink_to(root / relative)
    captured, resume = threading.Event(), threading.Event()
    original = get_page.get_page
    result = []

    def paused(*args, **kwargs):
        page = original(*args, **kwargs)
        if threading.current_thread().name == "ownership-page" and not captured.is_set():
            captured.set()
            assert resume.wait(15)
            if failed_read:
                raise ValueError("read failed: " + secret)
        return page

    monkeypatch.setattr(get_page, "get_page", paused)

    def read():
        try:
            result.append(call(root, "read_memory", path=requested))
        except Exception as error:  # noqa: BLE001 - transfer the public failure to the asserting thread
            result.append(error)

    with coordinator(tmp_path / "coordinator.sqlite"):
        reader = threading.Thread(target=read, name="ownership-page")
        reader.start()
        try:
            assert captured.wait(10)
            assert offline_create(root, tmp_path / "c.md", manifest_path(), why="first collection") == 0
        finally:
            resume.set()
            reader.join(15)
    assert not reader.is_alive()
    assert len(result) == 1 and isinstance(result[0], OpError), result
    assert result[0].code == "COLLECTION_STORE_UNAVAILABLE"
    events = _receipt_records(root)
    assert any(event["event_type"] == "credential_block" and event.get("command") == "read_memory" for event in events)
    assert not any(outcome["decision"] in ("released", "release_authorized")
                   for event in events if event["event_type"] == "disclosure" for outcome in event["outcomes"])
    assert secret not in str(result) + str(events)


def test_complete_negative_ownership_reads_no_row_or_projection_census(service, monkeypatch):
    from contextvars import ContextVar

    root = service.root
    path = "Knowledge Base/Records/Work/Independent.md"
    (root / path).write_text("# Ordinary file\n\nordinary-body\n")
    opened = connection._open_read_only
    attempted = []
    observed = set()
    foreground = ContextVar("negative_ownership_foreground", default=False)

    def no_rows(*args, **kwargs):
        conn, version = opened(*args, **kwargs)

        def authorize(action, table, *_):
            # These store tables contain rows or publications, not declaration ownership.
            if foreground.get() and action == sqlite3.SQLITE_READ:
                observed.add(table)
                if table in ("items", "held_candidates", "projection_state"):
                    attempted.append(table)
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorize)
        return conn, version

    monkeypatch.setattr(connection, "_open_read_only", no_rows)
    token = foreground.set(True)
    try:
        assert "ordinary-body" in call(root, "read_memory", path=path)["body"]
    finally:
        foreground.reset(token)
    assert observed, "the foreground ownership proof did not reach the read observer"
    assert attempted == []


def test_an_ordinary_row_save_does_not_invalidate_a_whole_overview(service, monkeypatch):
    root = service.root
    captured, resume = threading.Event(), threading.Event()
    walk = overview.os.walk
    result = []

    def paused(*args, **kwargs):
        if threading.current_thread().name == "ownership-overview":
            captured.set()
            assert resume.wait(15)
        yield from walk(*args, **kwargs)

    monkeypatch.setattr(overview.os, "walk", paused)

    def read():
        try:
            result.append(overview.overview(root))
        except Exception as error:  # noqa: BLE001 - report the public result on the asserting thread
            result.append(error)

    reader = threading.Thread(target=read, name="ownership-overview")
    reader.start()
    try:
        assert captured.wait(10)
        call(root, "record_memory", action="append", collection=CID,
             item={"title": "Concurrent row"}, why="capture")
    finally:
        resume.set()
        reader.join(15)
    assert not reader.is_alive()
    assert len(result) == 1 and isinstance(result[0], dict), result
    assert titles(root, CID) == ["Canonical", "Concurrent row"]


def test_recovery_must_cover_later_collections_before_and_after_a_whole_read(service, tmp_path, monkeypatch):
    root = service.root
    _published(SimpleNamespace(meta=lambda: store_meta(root)))
    old = tmp_path / "only-first.sqlite"
    owner.backup(root, destination=old)
    call(root, "record_memory", action="create", manifest_path=DAILY_PATH,
         manifest_text=DAILY_TEXT, why="second collection")
    call(root, "record_memory", action="append", collection=DAILY,
         item={"title": "Second canonical"}, item_key=KEY, why="capture")
    runtime.flush_for_export(root)
    service.stop()
    source = replica.replica_path(root)
    current = source.read_bytes()
    decoy = (root / DAILY_PATH).parent / "Items" / (KEY + ".md")
    decoy.parent.mkdir(parents=True, exist_ok=True)
    decoy.write_text("---\ntype: record\n---\n\nLater collection decoy.\n")
    note = root / "Knowledge Base/Notes/Independent.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Independent\n\nIndependent note.\n")
    sibling = root / "Knowledge Base/Records/Independent.md"
    sibling.write_text("# Ordinary sibling\n\nOrdinary sibling body.\n")
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "fresh-recovery-state"))
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "fresh-recovery-lease"))
    state_migration.reset_state_resolution_cache_for_tests()
    authority.marker_path(root).unlink()
    assert not connection.store_path(root).exists()

    def receive(data):
        staged = source.with_name("received-snapshot.sqlite")
        staged.write_bytes(data)
        staged.replace(source)

    receive(old.read_bytes())
    with pytest.raises(OpError, match="COLLECTION_STORE_UNAVAILABLE"):
        call(root, "read_memory", path=decoy.relative_to(root).as_posix())
    assert "Independent note." in call(root, "read_memory", path=note.relative_to(root).as_posix())["body"]

    receive(current)
    relative = sibling.relative_to(root).as_posix()
    assert "Ordinary sibling body." in call(root, "read_memory", path=relative)["body"]
    captured, resume = threading.Event(), threading.Event()
    original = get_page.get_page
    result = []

    def paused(*args, **kwargs):
        page = original(*args, **kwargs)
        if threading.current_thread().name == "recovery-page" and not captured.is_set():
            captured.set()
            assert resume.wait(15)
        return page

    monkeypatch.setattr(get_page, "get_page", paused)

    def read():
        try:
            result.append(call(root, "read_memory", path=relative))
        except Exception as error:  # noqa: BLE001 - transfer the public result to the asserting thread
            result.append(error)

    reader = threading.Thread(target=read, name="recovery-page")
    reader.start()
    try:
        assert captured.wait(10)
        receive(old.read_bytes())
    finally:
        resume.set()
        reader.join(15)
    assert not reader.is_alive()
    assert len(result) == 1 and isinstance(result[0], OpError), result
    assert result[0].code == "COLLECTION_STORE_UNAVAILABLE"
    assert "Ordinary sibling body." not in str(result)


def test_an_unrelated_dark_preview_cannot_join_first_production_admission(tmp_path, monkeypatch, capsys):
    root = _served_vault(tmp_path, monkeypatch)
    private_id = "88888888-8888-4888-8888-888888888888"
    with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as handle:
        with preview_store(root, handle) as writer:
            writer.create_collection(manifest_path().replace("Work", "Preview"),
                manifest_text().replace(CID, private_id), why="private preparation", scaffold=False)
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        before = list(reader.execute("SELECT key,value FROM store_meta ORDER BY key"))
    descriptors = state_migration.recorded_descriptor_ids(root)
    with coordinator(tmp_path / "coordinator.sqlite"):
        assert offline_create(root, tmp_path / "c.md", manifest_path(), why="first collection") == 1
    assert "COLLECTION_STORE_CREATE_CONFLICT" in capsys.readouterr().err
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        assert list(reader.execute("SELECT key,value FROM store_meta ORDER BY key")) == before
        assert reader.execute("SELECT collection_id FROM collections").fetchall() == [(private_id,)]
    assert state_migration.recorded_descriptor_ids(root) == descriptors
    assert authority.read_marker(root) is None


@pytest.mark.parametrize("hint", ["older", "absent"])
def test_replica_verification_checks_containment_of_collections_omitted_by_the_hint(service, hint):
    root = service.root
    older = authority.read_marker(root)
    call(root, "record_memory", action="create", manifest_path=DAILY_PATH,
         manifest_text=DAILY_TEXT, why="second collection")
    call(root, "record_memory", action="append", collection=DAILY,
         item={"title": "Second canonical"}, item_key=KEY, why="capture")
    _published(SimpleNamespace(meta=lambda: store_meta(root)))
    service.stop()
    if hint == "older":
        authority.marker_path(root).write_bytes(older)
    else:
        authority.marker_path(root).unlink()
    source = replica.replica_path(root)
    with sqlite3.connect(source) as writer:
        writer.execute("UPDATE items SET view_path=? WHERE collection_id=?",
                       ("Knowledge Base/Notes/Outside.md", DAILY))
    before = source.read_bytes()
    with pytest.raises(connection.CollectionStoreError, match="outside canonical ownership"):
        owner.adopt_replica_preview(root)
    with pytest.raises(connection.CollectionStoreError, match="outside canonical ownership"):
        with vault_portability.checked_collection_replica(root):
            pytest.fail("the omitted collection escaped its canonical namespace")
    assert source.read_bytes() == before


@pytest.mark.parametrize("hint", ["older", "absent"])
def test_owner_preview_binds_original_replica_bytes_despite_incomplete_hints(service, hint):
    root = service.root
    older = authority.read_marker(root)
    call(root, "record_memory", action="create", manifest_path=DAILY_PATH,
         manifest_text=DAILY_TEXT, why="second collection")
    _published(SimpleNamespace(meta=lambda: store_meta(root)))
    service.stop()
    if hint == "older":
        authority.marker_path(root).write_bytes(older)
    else:
        authority.marker_path(root).unlink()
    source = replica.replica_path(root)
    before = source.read_bytes()
    sealed = owner.adopt_replica_preview(root)
    preview = sealed["preview"]
    assert preview["store_id"] == store_meta(root)[schema.META_STORE_ID]
    assert preview["replica"]["sha256"] == hashlib.sha256(before).hexdigest()
    assert preview["marker_sha256"] == hashlib.sha256(older if hint == "older" else b"").hexdigest()
    assert source.read_bytes() == before


def test_verified_restore_requires_schema_13_before_any_local_store_admission(service, tmp_path, monkeypatch):
    root = service.root
    _published(SimpleNamespace(meta=lambda: store_meta(root)))
    service.stop()
    authority.marker_path(root).unlink()
    source = replica.replica_path(root)
    before = source.read_bytes()
    fresh_state = tmp_path / "fresh-restore-state"
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(fresh_state))
    assert not connection.store_path(root).exists()
    assert "collections-store-schema-13" in authority.required_state_compatibility_ids(root)
    assert state_migration.recorded_descriptor_ids(root) is None
    assert source.read_bytes() == before
    # The same old reader refusal applies before adoption can create host state.
    with monkeypatch.context() as older:
        older.setattr(schema, "SCHEMA_VERSION", 12)
        with pytest.raises(connection.CollectionStoreError, match="SCHEMA_NEWER"):
            connection.open_reader(source)
    assert not fresh_state.exists()
