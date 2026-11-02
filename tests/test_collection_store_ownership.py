"""Canonical ownership survives stale hints and concurrent first admission."""

import contextvars
import hashlib
import sqlite3
import threading
from contextlib import closing, contextmanager
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
from exomem.collection_store import (
    authority,
    connection,
    owner,
    ownership,
    replica,
    runtime,
    schema,
)
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


@pytest.mark.parametrize("profile", ("records", "planning"))
def test_first_admission_invalidates_a_standalone_query_before_its_receipt(tmp_path, monkeypatch, profile):
    from test_collection_store_service import A_PATH, B_PATH

    from exomem import planning, record_formats, record_governance
    from exomem.governance.principal import owner_principal, request_scope

    root = _served_vault(tmp_path, monkeypatch)
    captured, resume = threading.Event(), threading.Event()
    original = record_formats.query_collection
    result = []

    def paused(*args, **kwargs):
        value = original(*args, **kwargs)
        if threading.current_thread().name == "ownership-query":
            assert value.total_matched == 0
            captured.set()
            assert resume.wait(15)
        return value

    monkeypatch.setattr(record_formats, "query_collection", paused)

    def read():
        with request_scope(owner_principal(surface="cli")):
            try:
                value = (record_governance.query_collection(root, A_PATH) if profile == "records"
                         else planning.query(root, B_PATH, lifecycle="all"))
                result.append(value)
            except Exception as error:  # noqa: BLE001 - transfer the public result to the asserting thread
                result.append(error)

    with coordinator(tmp_path / "coordinator.sqlite"):
        reader = threading.Thread(target=read, name="ownership-query")
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
    assert not any(outcome.get("command") == "record_query"
                   and outcome["decision"] in ("released", "release_authorized")
                   for event in _receipt_records(root) if event["event_type"] == "disclosure"
                   for outcome in event["outcomes"])


@pytest.mark.parametrize("phase", ("during_query", "after_query"))
def test_first_admission_invalidates_a_composite_query_before_its_receipt(tmp_path, monkeypatch, phase):
    from test_collection_store_service import B_PATH
    from test_record_governance import _write_l6_rule

    from exomem import plan_progress, planning
    from exomem.governance.principal import RequestPrincipal, request_scope

    root = _served_vault(tmp_path, monkeypatch)
    _write_l6_rule(root, ceiling=6, paths="Planning/**")
    captured, resume = threading.Event(), threading.Event()
    target, name = ((planning, "_project_query") if phase == "during_query"
                    else (plan_progress, "_planning_page"))
    original = getattr(target, name)
    result = []
    evaluated = []

    def paused(*args, **kwargs):
        value = original(*args, **kwargs)
        if threading.current_thread().name == "ownership-progress":
            evaluated.append(value)
            captured.set()
            assert resume.wait(15)
        return value

    monkeypatch.setattr(target, name, paused)

    def read():
        with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
            try:
                result.append(plan_progress.review(root, collection=B_PATH))
            except Exception as error:  # noqa: BLE001 - transfer the public result to the asserting thread
                result.append(error)

    with coordinator(tmp_path / "coordinator.sqlite"):
        reader = threading.Thread(target=read, name="ownership-progress")
        reader.start()
        try:
            assert captured.wait(10), result
            assert offline_create(root, tmp_path / "c.md", manifest_path(), why="first collection") == 0
        finally:
            resume.set()
            reader.join(15)
    assert not reader.is_alive()
    assert evaluated and evaluated[0]["total_matched"] == 0
    assert len(result) == 1 and isinstance(result[0], OpError), result
    assert result[0].code == "COLLECTION_STORE_UNAVAILABLE"
    assert not any(outcome.get("command") == "record_query"
                   and outcome["decision"] in ("released", "release_authorized")
                   for event in _receipt_records(root) if event["event_type"] == "disclosure"
                   for outcome in event["outcomes"])


@pytest.mark.parametrize(("action", "boundary"), [
    ("resolve", "record_resolve"),
    ("inventory", "record_inspection"),
    ("manifest", "record_manifest"),
    ("history", "record_history"),
])
def test_first_admission_rejects_a_standalone_collection_read_before_its_receipt(tmp_path, monkeypatch, action, boundary):
    from test_collection_store_service import A_PATH
    from test_record_governance import _write_l6_rule

    from exomem import record_governance, record_history
    from exomem.governance import egress
    from exomem.governance.principal import RequestPrincipal, request_scope

    root = _served_vault(tmp_path, monkeypatch)
    _write_l6_rule(root, ceiling=6, paths="Records/**")
    item = (root / A_PATH).parent / "Items" / (KEY + ".md")
    item.write_text("---\ntype: record\ncollection_id: " + KEY + "\nrecord_id: " + KEY
                    + "\nschema_version: 1\ntitle: File row\n---\n")
    captured, resume = threading.Event(), threading.Event()
    result, evaluated = [], []
    if action == "resolve":
        target, name = record_governance, "_resolve_released_collection"
    elif action == "inventory":
        target, name = record_governance, "_inventory_coverage"
    else:
        target, name = egress, "project"
    original = getattr(target, name)

    def paused(*args, **kwargs):
        value = original(*args, **kwargs)
        if (threading.current_thread().name == "ownership-collection-read"
                and not captured.is_set()
                and (target is not egress or kwargs.get("kind") == boundary)):
            evaluated.append(value)
            captured.set()
            assert resume.wait(15)
        return value

    monkeypatch.setattr(target, name, paused)

    def read():
        with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
            try:
                if action == "resolve":
                    value = record_governance.resolve_collection(root, A_PATH)
                elif action == "inventory":
                    value = record_governance.inventory_collections(root)
                elif action == "manifest":
                    value = record_governance.project_manifest(root, A_PATH)
                else:
                    value = record_history.read(root, A_PATH, item_key=KEY, semantic_profile="records")
                result.append(value)
            except Exception as error:  # noqa: BLE001 - transfer the public result to the asserting thread
                result.append(error)

    with coordinator(tmp_path / "coordinator.sqlite"):
        reader = threading.Thread(target=read, name="ownership-collection-read")
        reader.start()
        try:
            assert captured.wait(10), result
            assert offline_create(root, tmp_path / "c.md", manifest_path(), why="first collection") == 0
        finally:
            resume.set()
            reader.join(15)
    assert not reader.is_alive()
    assert evaluated and evaluated[0]
    assert len(result) == 1 and isinstance(result[0], OpError), result
    assert result[0].code == "COLLECTION_STORE_UNAVAILABLE"
    assert not any(outcome.get("command") == boundary
                   and outcome["decision"] in ("released", "release_authorized")
                   for event in _receipt_records(root) if event["event_type"] == "disclosure"
                   for outcome in event["outcomes"])


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
    # The observer sits on the opener, and a pooled reader opened before it would skip it.
    runtime.reader_pool(root, connection.store_path(root)).close()

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


def test_a_pooled_proof_does_not_release_a_read_after_a_namespace_change(service, monkeypatch):
    """The release re-proves ownership on its own lend, which starts with no transaction, not from the body's snapshot."""
    root = service.root
    # The fixture's first row is still publishing; a request that starts meanwhile meets the handoff.
    _published(SimpleNamespace(meta=lambda: store_meta(root)))
    pool = runtime.reader_pool(root, connection.store_path(root))
    lent = []
    lend = pool.lend

    @contextmanager
    def recording(**kwargs):
        with lend(**kwargs) as reader:
            lent.append((threading.get_ident(), reader))
            yield reader

    monkeypatch.setattr(pool, "lend", recording)
    with ownership.read_scope(root) as scope:
        scope.proof()
        # A new request context, as the caller's own: the create must not share this read's scope.
        contextvars.Context().run(call, root, "record_memory", action="create", manifest_path=DAILY_PATH,
                                  manifest_text=DAILY_TEXT, why="second collection")
        with pytest.raises(OpError) as refused:
            scope.finish()
    assert refused.value.code == "COLLECTION_STORE_UNAVAILABLE"
    # The store thread's own reads share this pool, so the release may take a reader that thread returned last.
    last = max(index for index, (thread, _) in enumerate(lent) if thread == threading.get_ident())
    assert any(lent[last][1] is earlier for _, earlier in lent[:last]), "the release did not reuse a pooled reader"


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
    # Publish a later business head so this adoption fixture has no historical marker intent.
    call(root, "record_memory", action="append", collection=DAILY,
         item={"title": "Second canonical"}, item_key=KEY, why="capture")
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
    assert {"collections-store-schema-13", "collections-store-schema-14"} <= set(
        authority.required_state_compatibility_ids(root))
    assert state_migration.recorded_descriptor_ids(root) is None
    assert source.read_bytes() == before
    # The same old reader refusal applies before adoption can create host state.
    with monkeypatch.context() as older:
        older.setattr(schema, "SCHEMA_VERSION", 12)
        with pytest.raises(connection.CollectionStoreError, match="SCHEMA_NEWER"):
            connection.open_reader(source)
    assert not fresh_state.exists()


@pytest.mark.parametrize("forged", [None, "store_id", "instance_id", "head_hash"])
def test_ownership_trusts_no_store_chain_that_lacks_the_recorded_head(service, monkeypatch, forged):
    """Defect: ownership trusts a negative from a store chain that does not hold the coordinator's recorded head."""
    import uuid
    from dataclasses import replace

    from exomem.collection_store import ownership

    root = service.root
    sibling = root / "Knowledge Base/Records/Independent.md"
    sibling.write_text("# Ordinary sibling\n\nOrdinary sibling body.\n")
    note = root / "Knowledge Base/Notes/Independent.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Independent\n\nIndependent note.\n")
    meta = store_meta(root)
    head = writer_lease.CollectionStoreHead(
        meta[schema.META_STORE_ID], meta[schema.META_INSTANCE_ID], int(meta[schema.META_COMMIT_SEQ]),
        meta[schema.META_STORE_HEAD_HASH])
    if forged is not None:
        head = replace(head, **{forged: "0" * 64 if forged == "head_hash" else str(uuid.uuid4())})
    real = ownership._manager(root)

    class Client:
        # Only the coordinator's recorded head changes, as ownership reads it; the lease keeps the real client.
        def __getattr__(self, name):
            return getattr(real.client, name)

        def status(self):
            return replace(real.client.status(), collection_store_head=head)

    class Manager:
        client = Client()

        def __getattr__(self, name):
            return getattr(real, name)

    read = lambda path: call(root, "read_memory", path=path.relative_to(root).as_posix())  # noqa: E731
    # Scoped, so the service fixture's teardown sees the real coordinator head.
    with monkeypatch.context() as patch:
        patch.setattr(ownership, "_manager", lambda _root: Manager())
        if forged is None:
            assert "Ordinary sibling body." in read(sibling)["body"]
        else:
            with pytest.raises(OpError, match="COLLECTION_STORE_UNAVAILABLE"):
                read(sibling)
        assert "Independent note." in read(note)["body"]


def test_a_proof_after_a_committed_create_routes_the_new_collection(service):
    """Defect: a proof reuses the parse of the declarations it made before a create, so ownership keeps
    answering for the earlier namespace and a read of the new collection falls through to its view files."""
    root = service.root
    assert ownership.current(root).owner(root, DAILY_PATH) is None
    call(root, "record_memory", action="create", manifest_path=DAILY_PATH, manifest_text=DAILY_TEXT,
         why="second collection")
    proof = ownership.current(root)
    assert sorted(entry["collection_id"] for entry in proof.entries) == sorted([CID, DAILY])
    assert proof.owner(root, DAILY_PATH)["collection_id"] == DAILY


def test_a_proof_refuses_a_manifest_whose_text_no_longer_matches_its_hash(service):
    """Defect: a proof trusts the parse it cached for a manifest, so text edited under its recorded hash still
    proves ownership."""
    root = service.root
    assert ownership.current(root).selected(root, CID) is not None
    with closing(sqlite3.connect(connection.store_path(root))) as raw:
        # The store refuses this edit while its triggers stand; a tamperer with file access removes them first.
        for (trigger,) in raw.execute("SELECT name FROM sqlite_master WHERE type='trigger' "
                                      "AND tbl_name='collection_manifests'").fetchall():
            raw.execute(f'DROP TRIGGER "{trigger}"')
        raw.execute("UPDATE collection_manifests SET manifest_text=manifest_text || ' ' WHERE collection_id=?", (CID,))
        raw.commit()
    with pytest.raises(OpError) as refused:
        ownership.current(root)
    assert refused.value.code == "COLLECTION_STORE_UNAVAILABLE"
