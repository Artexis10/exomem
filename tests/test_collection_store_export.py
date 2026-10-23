"""The reverse export of a migrated vault back to files (P1b.4), over the real store and coordinator.

The vault is ``legacy_vault``'s: four migrated collections in every shape the migration
carries. A service writes through the store, then the owner exports with the service
stopped. File mode must then read each collection as the store served it.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import shutil
import sqlite3
import uuid
from contextlib import closing, suppress
from dataclasses import replace
from pathlib import Path

import pytest
from test_collection_store_migration import (
    CID,
    KEY,
    LEDGER_ID,
    MIGRATED,
    OPERATOR,
    OTHER,
    PLANNING,
    WORK,
    X3,
    X3_ID,
    env,  # noqa: F401 - the fixture
    fence,
    legacy_vault,
    migrate,
    printed,
    rows,
    s1,  # noqa: F401 - the fixture
    start_service,
    vault_bytes,
    wait_out_the_lease,
)
from test_collection_store_s1_gate import coordinator
from test_collection_store_service import DAILY, Service, _until, call

from exomem import __main__ as cli
from exomem import record_formats, records, state_migration, state_paths, writer_lease
from exomem.cli_ops import OpError
from exomem.collection_store import (
    authority,
    capability,
    chain,
    connection,
    export,
    replica,
    schema,
)
from exomem.collection_store.connection import CollectionStoreError
from exomem.governance.principal import library_scope

COLLECTIONS = "Knowledge Base/_Collections"
#: A Records log row in the shape of the invented X3 fixture.
SESSION = {"occurred_on": "2026-08-03", "title": "Pull", "status": "completed",
           "movements": [{"movement": "Deadlift", "band": "grey", "repetitions": "22"}]}


def _view_is_stamped(path):
    """Publication can move the old view aside before its no-clobber install."""
    try:
        return b"exomem_view:" in path.read_bytes()
    except FileNotFoundError:
        return False


def run_export(root, *flags, operator=True):
    """`exomem collections export`, the operator credential set only in the apply shell."""
    with pytest.MonkeyPatch.context() as shell:
        if operator:
            shell.setenv(OPERATOR, "operator")
        return cli._collections_main(["export", "--vault", str(root), *flags])


def write_through_the_store(root):
    """A service writes a new Records item, corrects an imported one, appends a log row and plans an area."""
    service = start_service(root)
    try:
        call(root, "record_memory", action="append", collection=WORK, item={"title": "Store row", "count": 5},
             why="a store-mode write")
        original = next(row for row in call(root, "record_memory", action="query", collection=WORK)["rows"]
                        if row["record_id"] == KEY)
        guards = call(root, "record_memory", action="inspect", collection=WORK)["lifecycle_guards"]
        call(root, "record_memory", action="update", collection=WORK, item_key=KEY, changes={"count": 4},
             expected_item_version=original["item_version"],
             expected_container_hash=guards["expected_container_hash"], why="a store-mode correction")
        call(root, "record_memory", action="append", collection=X3, item=SESSION, why="a store-mode session")
        call(root, "plan_memory", action="add", collection=PLANNING, item={"title": "Operations", "kind": "area"},
             why="a store-mode plan")
        return {cid: rows(root, path) for cid, path in MIGRATED.items()}
    finally:
        service.stop()


def migrated_and_written(root, capsys):
    """The legacy vault migrated, then written through the store; its bytes before and what it served."""
    legacy_vault(root)
    before = vault_bytes(root)
    assert migrate(root, "apply") == 0 and printed(capsys)["status"] == "marker_admitted"
    return before, write_through_the_store(root)


def preview_id(root, capsys):
    assert run_export(root, "--dry-run", operator=False) == 0
    previewed = printed(capsys)
    assert previewed["verdict"] == "exportable", previewed
    return previewed["plan_id"]


def test_an_unwritten_collection_exports_exact_originals_after_view_stamping(env, capsys, monkeypatch):  # noqa: F811
    """First publication must not destroy byte-exact rollback of imported, untouched files."""
    root = env
    legacy_vault(root)
    original = root / "Knowledge Base/Records/Work/Items/Original.md"
    original.write_bytes(original.read_bytes().replace(b"title: One", b'title: "One"').replace(b"\n", b"\r\n"))
    manifest = root / WORK
    manifest.write_bytes(manifest.read_bytes().replace(b"\n", b"\r\n"))
    before = vault_bytes(root)
    selected = MIGRATED[LEDGER_ID]
    reference = call(root, "record_memory", action="query", collection=selected, include_agent_history=True)
    transitions = {event["transition_id"] for event in reference["agent_history"]["events"]}
    assert transitions
    assert migrate(root, "apply") == 0 and printed(capsys)["status"] == "marker_admitted"
    monkeypatch.setattr(capability, "RELEASED", capability.RELEASED | {
        capability.COLLECTION_VIEWS_V1, capability.RECORDS_SUMMARY_V1})
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        paths = [selected, *(path for path, in reader.execute("SELECT view_path FROM items WHERE collection_id=?",
                                                            (LEDGER_ID,)))]
    service = start_service(root)
    try:
        _until(lambda: all(_view_is_stamped(root / path) for path in paths))
        queried = call(root, "record_memory", action="query", collection=selected, include_agent_history=True)
        assert queried["source_versions"]
        assert {event["transition_id"] for event in queried["agent_history"]["events"]} == transitions
    finally:
        service.stop()
    plan_id = preview_id(root, capsys)
    assert run_export(root, "--plan-id", plan_id, "--why", "return untouched collections to files") == 0
    applied = printed(capsys)
    assert all(row["status"] == "unwritten" for row in applied["collections"])
    assert {path: digest for path, digest in vault_bytes(root).items() if path in before} == before


def test_a_legacy_view_moved_before_first_stamp_keeps_its_path_and_accepts_an_owner_edit(env, capsys, monkeypatch):  # noqa: F811
    """Startup must bind an untouched legacy rename before publishing or adopting a later edit."""
    root = env
    legacy_vault(root)
    assert migrate(root, "apply") == 0 and printed(capsys)["status"] == "marker_admitted"
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        original = reader.execute("SELECT view_path FROM items WHERE collection_id=? AND item_key=?", (CID, KEY)).fetchone()[0]
    source = root / original
    destination = source.with_name("Renamed.md")
    source.rename(destination)
    monkeypatch.setattr(capability, "RELEASED", capability.RELEASED | {
        capability.COLLECTION_VIEWS_V1, capability.RECORDS_SUMMARY_V1})
    service = start_service(root)
    try:
        _until(lambda: _view_is_stamped(destination))
        assert not source.exists()
        queried = call(root, "record_memory", action="query", collection=WORK)
        assert any(entry["path"] == destination.relative_to(root).as_posix() for entry in queried["source_versions"])
        service.stop()
        original_text = destination.read_text()
        edited = original_text.replace("count: 3", "count: 5")
        assert edited != original_text
        destination.write_text(edited)
        service = start_service(root)
        _until(lambda: call(root, "record_memory", action="query", collection=WORK)["rows"][0]["count"] == 5)
        assert call(root, "record_memory", action="inspect", collection=WORK)["projection"]["held_view_corrections"] == 0
        assert not source.exists()
    finally:
        service.stop()


def _crash_before_original_replica(environment, database, released):
    """Die after durable local backfill, before publication copies it to the vault."""
    os.environ.clear()
    os.environ.update(environment)
    capability.RELEASED = released
    publish = replica.publish_replica

    def cut(*args, **kwargs):
        if kwargs.get("force"):
            os._exit(73)
        return publish(*args, **kwargs)

    replica.publish_replica = cut
    with coordinator(database), library_scope():
        start_service(Path(environment["EXOMEM_VAULT_PATH"]))
    raise AssertionError("the publication cut was not reached")


def test_an_edited_unstamped_copy_keeps_a_legacy_rename_ambiguous(env, capsys, monkeypatch):  # noqa: F811
    """The matching original digest cannot choose between two destinations with one item identity."""
    root = legacy_vault(env)
    assert migrate(root, "apply") == 0 and printed(capsys)["status"] == "marker_admitted"
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        original = reader.execute("SELECT view_path FROM items WHERE collection_id=? AND item_key=?", (CID, KEY)).fetchone()[0]
    source = root / original
    raw = source.read_bytes()
    exact, edited = source.with_name("A.md"), source.with_name("B.md")
    source.rename(exact)
    changed = raw.replace(b"count: 3", b"count: 9")
    assert changed != raw
    edited.write_bytes(changed)
    monkeypatch.setattr(capability, "RELEASED", capability.RELEASED | {
        capability.COLLECTION_VIEWS_V1, capability.RECORDS_SUMMARY_V1})
    service = start_service(root)
    try:
        _until(lambda: call(root, "record_memory", action="inspect", collection=WORK)["projection"]["held_view_corrections"])
        with closing(connection.open_reader(connection.store_path(root))) as reader:
            assert reader.execute("SELECT view_path FROM items WHERE collection_id=? AND item_key=?", (CID, KEY)).fetchone()[0] == original
            assert reader.execute("SELECT COUNT(*) FROM txns WHERE collection_id=? AND operation='view_move'", (CID,)).fetchone()[0] == 0
        assert exact.read_bytes() == raw and edited.read_bytes() == changed
    finally:
        service.stop()


def test_a_backfill_interrupted_before_replication_retries_before_stamping_originals(env, capsys, monkeypatch, tmp_path):  # noqa: F811
    """A same-head retry must copy archived originals before publication destroys their only file copy."""
    root = legacy_vault(env)
    before = vault_bytes(root)
    assert migrate(root, "apply") == 0 and printed(capsys)["status"] == "marker_admitted"
    # Older schema-13 imports have the same business chain without the additive original-byte metadata.
    replica_path = replica.replica_path(root)
    for path in (replica_path, connection.store_path(root)):
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("DELETE FROM store_meta WHERE key LIKE 'legacy-original:%'")
    with closing(sqlite3.connect(connection.store_path(root))) as conn, conn:
        conn.execute("UPDATE store_meta SET value=? WHERE key=?",
                     (hashlib.sha256(replica_path.read_bytes()).hexdigest(), schema.META_LAST_PUBLISHED_REPLICA_SHA256))
    monkeypatch.setattr(capability, "RELEASED", capability.RELEASED | {
        capability.COLLECTION_VIEWS_V1, capability.RECORDS_SUMMARY_V1})
    child = multiprocessing.get_context("spawn").Process(target=_crash_before_original_replica,
        args=(dict(os.environ), tmp_path / "coordinator.sqlite", capability.RELEASED))
    try:
        child.start()
        child.join(30)
        assert child.exitcode == 73
    finally:
        if child.is_alive():
            child.terminate()
            child.join(10)
    wait_out_the_lease()
    assert {path: digest for path, digest in vault_bytes(root).items() if path in before} == before
    service = start_service(root)
    try:
        _until(lambda: _view_is_stamped(root / WORK))
        with closing(connection.open_reader(replica_path)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM store_meta WHERE key LIKE 'legacy-original:%'").fetchone()[0] > 0
    finally:
        service.stop()
    plan_id = preview_id(root, capsys)
    assert run_export(root, "--plan-id", plan_id, "--why", "restore imported bytes after an interrupted upgrade") == 0
    assert all(row["status"] == "unwritten" for row in printed(capsys)["collections"])
    assert {path: digest for path, digest in vault_bytes(root).items() if path in before} == before


def canonical_rows(root):
    """Every canonical and history table; lease and replica bookkeeping live in store_meta."""
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        tables = [name for name, in reader.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name!='store_meta' ORDER BY name")]
        return {name: sorted(reader.execute('SELECT * FROM "' + name.replace('"', '""') + '"'), key=repr)
                for name in tables}


@pytest.fixture
def published_views(env, monkeypatch):  # noqa: F811 - imported fixture
    """Exercise real view publication through its existing release capability."""
    monkeypatch.setattr(capability, "RELEASED", capability.RELEASED | {capability.RECORDS_SUMMARY_V1})
    return env


@pytest.mark.parametrize("hint", ["absent", "subset"])
def test_export_uses_every_canonical_collection_when_the_compatibility_hint_is_incomplete(env, capsys, hint):  # noqa: F811
    root = env
    legacy_vault(root)
    assert migrate(root, "apply") == 0
    capsys.readouterr()
    raw = authority.read_marker(root)
    if hint == "absent":
        authority.marker_path(root).unlink()
    else:
        marker = json.loads(raw)
        marker["collections"] = marker["collections"][:1]
        authority.marker_path(root).write_text(json.dumps(marker))
    plan_id = preview_id(root, capsys)
    assert run_export(root, "--plan-id", plan_id, "--why", "return to files") == 0
    report = printed(capsys)
    assert report["verdict"] == "exported"
    assert {row["collection_id"] for row in report["collections"]} == set(MIGRATED)
    assert authority.read_marker(root) is None
    assert not fence().enrolled


def test_a_cut_after_export_hint_repair_preserves_canonical_data_and_records_no_intent(env, capsys, monkeypatch):  # noqa: F811
    root = env
    legacy_vault(root)
    assert migrate(root, "apply") == 0
    capsys.readouterr()
    authority.marker_path(root).unlink()
    plan_id = preview_id(root, capsys)
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        before = [list(reader.execute("SELECT * FROM " + table)) for table in ("collections", "items")]
    replace_hint = export.admission._replace_marker

    def interrupted(*args):
        replace_hint(*args)
        raise RuntimeError("interrupted after hint repair")

    monkeypatch.setattr(export.admission, "_replace_marker", interrupted)
    with pytest.raises(RuntimeError, match="interrupted after hint repair"):
        run_export(root, "--plan-id", plan_id, "--why", "return to files")
    assert {entry["collection_id"] for entry in json.loads(authority.read_marker(root))["collections"]} == set(MIGRATED)
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        assert authority.pending_create(reader) is None
        assert [list(reader.execute("SELECT * FROM " + table)) for table in ("collections", "items")] == before
    assert fence().enrolled


@pytest.mark.parametrize("finish", ["resume", "abandon"])
def test_interrupted_history_retirement_keeps_its_bindings_until_export_or_regeneration(published_views, capsys, monkeypatch, finish):
    root = published_views
    migrated_and_written(root, capsys)
    plan_id = preview_id(root, capsys)
    removed = []
    remove = export._remove

    def interrupted(session, path, sha):
        remove(session, path, sha)
        if path.endswith("/_history.md") and not removed:
            removed.append(path)
            raise RuntimeError("interrupted after history retirement")

    with monkeypatch.context() as crash:
        crash.setattr(export, "_remove", interrupted)
        with pytest.raises(RuntimeError, match="interrupted after history retirement"):
            run_export(root, "--plan-id", plan_id, "--why", "return to files")
    assert len(removed) == 1 and not (root / removed[0]).exists()
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        assert authority.pending_create(reader)["kind"] == authority.EXPORT
        binding = reader.execute("SELECT published_sha256,pending_sha256 FROM projection_state WHERE path=?",
                                 (removed[0],)).fetchone()
        assert binding is not None and any(binding)
    if finish == "resume":
        assert run_export(root, "--plan-id", plan_id, "--why", "return to files") == 0
        assert printed(capsys)["status"] == "exported"
        assert not list(root.rglob("_history.md"))
    else:
        assert run_export(root, "--abandon", "--plan-id", plan_id, "--why", "keep canonical history") == 0
        assert printed(capsys)["status"] == "abandoned"
        assert not (root / removed[0]).exists()
        with closing(connection.open_reader(connection.store_path(root))) as reader:
            assert reader.execute("SELECT state FROM projection_state WHERE path=?", (removed[0],)).fetchone() == ("pending",)
            live_head = chain.verify_store_chain(reader)
        with closing(connection.open_reader(replica.replica_path(root))) as reader:
            assert chain.verify_store_chain(reader) == live_head
            assert reader.execute("SELECT state FROM projection_state WHERE path=?", (removed[0],)).fetchone() == ("pending",)
        lease = writer_lease.LeaseCoordinatorClient(writer_lease.LeaseConfig.from_env()).status()
        assert lease.holder is None
        assert (lease.collection_store_head.commit_seq, lease.collection_store_head.head_hash) == live_head
        service = start_service(root)
        try:
            _until(lambda: (root / removed[0]).is_file())
        finally:
            service.stop()


def test_unknown_and_edited_history_bytes_block_export_without_changing_them(published_views, capsys):
    root = published_views
    migrated_and_written(root, capsys)
    page = root / WORK.replace("_collection.md", "_history.md")
    unknown = page.parent / "_history" / "unregistered.md"
    unknown.parent.mkdir(parents=True, exist_ok=True)
    unknown.write_text("Authored history note.\n")
    assert run_export(root, "--dry-run", operator=False) == 1
    assert export.VIEW_EDITED in capsys.readouterr().out
    assert unknown.read_text() == "Authored history note.\n"
    unknown.unlink()
    edited = page.read_bytes() + b"\nAn authored correction.\n"
    page.write_bytes(edited)
    assert run_export(root, "--dry-run", operator=False) == 1
    assert export.VIEW_EDITED in capsys.readouterr().out
    assert page.read_bytes() == edited and fence().enrolled


def test_export_returns_a_migrated_vault_to_files_file_mode_reads_as_the_store_served(published_views, capsys):
    """An export that loses or changes a row the store served, rewrites a collection the store
    never wrote, leaves a written collection without its acknowledged checkpoint, or leaves
    anything that routes, fences or adopts the store: the marker, the fence, the descriptors,
    the replica or the live store under its adoptable name."""
    root = published_views
    before, served = migrated_and_written(root, capsys)
    store_id = authority.parse_marker(root, authority.read_marker(root))["store_id"]
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        history_paths = [row[0] for row in reader.execute("SELECT path FROM projection_state WHERE kind='history'")]
        transitions = list(reader.execute("SELECT transition_id FROM txns ORDER BY txn_id"))
    assert history_paths and any((root / path).is_file() for path in history_paths)
    plan_id = preview_id(root, capsys)

    assert run_export(root, "--plan-id", plan_id, "--why", "roll back to files") == 0
    applied = printed(capsys)

    assert (applied["status"], applied["store_id"], applied["retired_fence_generation"]) == (
        "exported", store_id, 1)
    assert {row["collection_id"]: row["status"] for row in applied["collections"]} == {
        CID: "written", X3_ID: "written", LEDGER_ID: "unwritten", OTHER: "written"}
    at = applied["exported_at"]
    assert authority.read_marker(root) is None
    assert sorted(path.name for path in (root / COLLECTIONS).iterdir()) == [
        f"collections.sqlite.exported-{at}", f"mode.json.exported-{at}"]
    assert not connection.store_path(root).exists()
    assert (state_paths.vault_state_dir(root) / f"collections.sqlite.exported-{at}").is_file()
    assert not any(os.path.lexists(root / path) for path in history_paths)
    with closing(connection.open_reader(state_paths.vault_state_dir(root) / f"collections.sqlite.exported-{at}")) as reader:
        assert list(reader.execute("SELECT transition_id FROM txns ORDER BY txn_id")) == transitions
    assert (fence().enrolled, fence().generation) == (False, 1)
    assert not {"collections-store-v1", state_migration.COLLECTION_MARKER_COMPATIBILITY_ID} & set(
        state_migration.recorded_descriptor_ids(root))
    unwritten = "Knowledge Base/Records/Publications/"
    assert {name: digest for name, digest in vault_bytes(root).items() if name.startswith(unwritten)} == {
        name: digest for name, digest in before.items() if name.startswith(unwritten)}
    # Without a marker, the dispatcher reads every collection from its files.
    assert {cid: rows(root, path) for cid, path in MIGRATED.items()} == served
    from test_governance_egress import _external, _through_dispatcher, write_rule, write_scope

    from exomem.governance.principal import request_scope

    write_scope(root, paths="Records/**")
    write_rule(root, ceiling=6)
    with request_scope(_external()), pytest.raises(ValueError, match="NOT_FOUND"):
        _through_dispatcher(root, "get", path=WORK.replace("_collection.md", "_history.md"))
    audit = {cid: call(root, "plan_memory" if path == PLANNING else "record_memory", action="inspect",
                       collection=path)["audit"]["status"] for cid, path in MIGRATED.items()}
    assert audit == {CID: "acknowledged_gap", X3_ID: "acknowledged_gap", LEDGER_ID: "ok",
                     OTHER: "acknowledged_gap"}
    with closing(connection.open_reader(state_paths.vault_state_dir(root) / f"collections.sqlite.exported-{at}")) \
            as tombstone:
        intent = json.loads(tombstone.execute("SELECT value FROM store_meta WHERE key=?",
                                              (authority.PENDING_CREATE,)).fetchone()[0])
    assert (intent["kind"], intent["plan_id"]) == (authority.EXPORT, plan_id)
    assert export.EXPORTED in applied["marker"]


def test_an_export_also_retires_the_store_schema_ids(env, capsys, monkeypatch):  # noqa: F811
    """Defect: an export leaves a store schema ID recorded, so the earlier release that the
    export-first downgrade path names still refuses the exported vault at its startup."""
    from exomem.collection_store import schema

    # Schema 12 is the first with an ID; give the current schema one, as a store migrated to 12 would have.
    monkeypatch.setattr(schema, "FIRST_GUARDED_SCHEMA_VERSION", schema.SCHEMA_VERSION)
    root = env
    migrated_and_written(root, capsys)
    # What the writer records before it migrates the store from an earlier schema.
    state_migration.record_collection_store_schema(root, authority_check=lambda: True)
    guard = set(schema.compatibility_ids())
    assert guard and guard <= set(state_migration.recorded_descriptor_ids(root))

    assert run_export(root, "--plan-id", preview_id(root, capsys), "--why", "roll back to files") == 0

    assert not guard & set(state_migration.recorded_descriptor_ids(root))


def exported(root, capsys):
    """The migrated, written vault exported by its owner; the apply's report."""
    migrated_and_written(root, capsys)
    assert run_export(root, "--plan-id", preview_id(root, capsys), "--why", "roll back to files") == 0
    return printed(capsys)


def file_service(root):
    """A service started on a vault that routes nothing to a store, as after an export."""
    return Service(root, writer_lease.start_server_lifecycle())


def test_the_exporting_host_serves_files_and_never_reopens_its_tombstoned_store(env, capsys):  # noqa: F811
    """An export after which the service reopens, recreates or serves the store, or refuses a
    file-mode write because a fence or marker still names the store."""
    root = env
    at = exported(root, capsys)["exported_at"]
    tombstones = vault_bytes(root, outside="Knowledge Base/Records")
    service = file_service(root)
    try:
        written = call(root, "record_memory", action="append", collection=WORK, item={"title": "File row"},
                       why="a file-mode write after the export")
        assert written["outcome"] == "committed" and (root / written["paths"][0]).is_file()
        assert "File row" in {row["title"] for row in call(root, "record_memory", action="query",
                                                             collection=WORK)["rows"]}
    finally:
        service.stop()
    assert not connection.store_path(root).exists()
    assert (state_paths.vault_state_dir(root) / f"collections.sqlite.exported-{at}").is_file()
    assert {path: digest for path, digest in vault_bytes(root, outside="Knowledge Base/Records").items()
            if path.startswith(COLLECTIONS)} == {path: digest for path, digest in tombstones.items()
                                                 if path.startswith(COLLECTIONS)}


def test_a_copied_vault_on_fresh_state_adopts_no_tombstoned_replica(env, capsys, tmp_path, monkeypatch):  # noqa: F811
    """A host meeting the exported vault for the first time that serves it through a store, by
    adopting the tombstoned replica or refusing a stale marker, instead of reading the files."""
    root = env
    exported(root, capsys)
    copy = tmp_path / "copy"
    shutil.copytree(root, copy)
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "fresh-state"))
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(copy))
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "lease-copy"))
    state_migration.require_vault_state_ready(copy)
    service = file_service(copy)
    try:
        assert {row["title"] for row in call(copy, "record_memory", action="query", collection=WORK)["rows"]} == {
            "One", "Store row"}
    finally:
        service.stop()
    assert not connection.store_path(copy).exists()
    assert authority.read_marker(copy) is None


def test_a_host_with_a_stale_store_serves_files_and_retire_local_waits_for_the_exported_vault(
        env, capsys, tmp_path, monkeypatch):  # noqa: F811
    """A second host that keeps serving or adopting its pre-export copy of the store, or a
    retire-local that tombstones a store its vault copy still routes to."""
    root = env
    migrated_and_written(root, capsys)
    other = tmp_path / "other"
    shutil.copytree(root, other)
    host = {"EXOMEM_STATE_ROOT": str(tmp_path / "other-state"), "EXOMEM_VAULT_PATH": str(other),
            "EXOMEM_WRITER_LEASE_STATE_DIR": str(tmp_path / "lease-other"),
            "EXOMEM_WRITER_LEASE_REPLICA_ID": "other"}
    with pytest.MonkeyPatch.context() as there:
        for name, value in host.items():
            there.setenv(name, value)
        start_service(other).stop()  # it adopts the replica: this host now keeps a store of its own
        assert connection.store_path(other).exists()
    # Receiving the newer tenure does not yet admit the exporting host's older local SQL.
    shutil.copy2(other / COLLECTIONS / "collections.sqlite", root / COLLECTIONS / "collections.sqlite")
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        local_before = list(reader.execute("SELECT key,value FROM store_meta ORDER BY key"))
    assert run_export(root, "--dry-run", operator=False) == 1
    assert "COLLECTION_STORE_UNAVAILABLE" in capsys.readouterr().err
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        assert list(reader.execute("SELECT key,value FROM store_meta ORDER BY key")) == local_before
    start_service(root).stop()
    assert run_export(root, "--plan-id", preview_id(root, capsys), "--why", "roll back to files") == 0
    at = printed(capsys)["exported_at"]
    for name, value in host.items():
        monkeypatch.setenv(name, value)
    assert run_export(other, "--retire-local", operator=False) == 1  # the exported vault has not reached here yet
    assert "still routes" in capsys.readouterr().err and connection.store_path(other).exists()
    shutil.rmtree(other)
    shutil.copytree(root, other)  # the exported vault reaches the other host
    paired = other / COLLECTIONS / (connection.STORE_FILENAME + export.EXPORTED + at)
    saved = paired.with_name(paired.name + ".saved")
    paired.rename(saved)
    try:
        with pytest.raises(OpError, match="COLLECTION_STORE_UNAVAILABLE"):
            call(other, "record_memory", action="query", collection=WORK)
        assert run_export(other, "--retire-local", operator=False) == 1
        assert "completed export evidence is unavailable" in capsys.readouterr().err
        assert connection.store_path(other).exists()
    finally:
        saved.rename(paired)
    service = file_service(other)
    try:
        assert {row["title"] for row in call(other, "record_memory", action="query", collection=WORK)["rows"]} == {
            "One", "Store row"}
    finally:
        service.stop()
    assert connection.store_path(other).exists()  # served from files, never opened

    assert run_export(other, "--retire-local", operator=False) == 0  # the writer credential suffices
    assert printed(capsys)["status"] == "retired"
    assert not connection.store_path(other).exists()
    assert (state_paths.vault_state_dir(other) / f"collections.sqlite.exported-{at}").is_file()
    assert not {"collections-store-v1", state_migration.COLLECTION_MARKER_COMPATIBILITY_ID} & set(
        state_migration.recorded_descriptor_ids(other))


def _export_and_crash(environment, database, cut, plan_id, flags):
    """`collections export --plan-id` in a fresh interpreter that dies at ``cut``, as a killed process would."""
    os.environ.clear()
    os.environ.update(environment)
    records._capture_sweep_carrier = records._due_state_carrier = lambda *a, **kw: None
    from exomem.collection_store import admission

    def die(*args, **kwargs):
        os._exit(73)

    def after(original):
        def run(*args, **kwargs):
            original(*args, **kwargs)
            os._exit(73)
        return run

    def at_work(original):
        def run(root, manifest_path, **kwargs):
            if manifest_path == WORK:
                os._exit(73)
            return original(root, manifest_path, **kwargs)
        return run

    {"after-begin": lambda: setattr(export, "_begin", after(export._begin)),
     "before-files": lambda: setattr(export, "_write_collection", die),
     "between-files-and-checkpoint": lambda: setattr(records, "export_checkpoint", die),
     # The other written collections, which come first, have their files and their checkpoints.
     "between-work-files-and-checkpoint": lambda: setattr(records, "export_checkpoint",
                                                          at_work(records.export_checkpoint)),
     "after-fence-retire": lambda: setattr(export, "_detach", die),
     "after-marker-retire": lambda: setattr(export, "_retire_marker", after(export._retire_marker)),
     "before-store-tombstone": lambda: setattr(export, "_tombstone_store", die),
     "after-abandon-transaction": lambda: setattr(admission, "_publish_current_epoch", die)}[cut]()
    with coordinator(database), library_scope():
        cli._collections_main(["export", "--vault", environment["EXOMEM_VAULT_PATH"], "--plan-id", plan_id,
                               "--why", "roll back to files", *flags])
    os._exit(0)


def crash_export(tmp_path, cut, plan_id, *flags):
    """Run one export apply (or ``flags``' step) that dies at ``cut``, then wait out the dead owner's lease."""
    import multiprocessing

    child = multiprocessing.get_context("spawn").Process(target=_export_and_crash, args=(
        {**os.environ, OPERATOR: "operator"}, tmp_path / "coordinator.sqlite", cut, plan_id, flags))
    child.start()
    child.join(120)
    assert child.exitcode == 73
    wait_out_the_lease()


@pytest.mark.parametrize("cut", ["after-begin", "before-files", "between-files-and-checkpoint", "after-fence-retire",
                                 "after-marker-retire", "before-store-tombstone"])
def test_an_export_cut_at_any_step_resumes_from_its_intent_to_the_same_files(env, capsys, tmp_path, cut):  # noqa: F811
    """An export killed between two steps that cannot finish, finishes differently, or, cut after
    its fence retire, leaves the retired fence beside a marker that still routes collections."""
    root = env
    _, served = migrated_and_written(root, capsys)
    plan_id = preview_id(root, capsys)
    crash_export(tmp_path, cut, plan_id)
    if cut == "after-fence-retire":
        assert not fence().enrolled and authority.read_marker(root) is not None
    assert run_export(root, "--dry-run", operator=False) == 0
    assert (printed(capsys)["verdict"], connection.store_path(root).exists()) == ("export_pending", True)

    assert run_export(root, "--plan-id", plan_id, "--why", "roll back to files") == 0
    resumed = printed(capsys)

    assert (resumed["status"], resumed["plan_id"]) == ("exported", plan_id)
    assert authority.read_marker(root) is None and not connection.store_path(root).exists()
    assert (fence().enrolled, fence().generation) == (False, 1)
    assert not {"collections-store-v1", state_migration.COLLECTION_MARKER_COMPATIBILITY_ID} & set(
        state_migration.recorded_descriptor_ids(root))
    assert {cid: rows(root, path) for cid, path in MIGRATED.items()} == served
    assert call(root, "record_memory", action="inspect", collection=WORK)["audit"]["status"] == "acknowledged_gap"
    checkpoints = [line for line in (root / "Knowledge Base/log.md").read_text().splitlines()
                   if "rebaseline" in line and "collections-store-history" in line]
    assert len(checkpoints) == 3  # one per written collection, never repeated by the resume


def another_host_writes(root, tmp_path):
    """A second host with its own copy of the vault adopts the replica and writes a row through its store."""
    other = tmp_path / "other"
    shutil.copytree(root, other)
    with pytest.MonkeyPatch.context() as there:
        for name, value in {"EXOMEM_STATE_ROOT": str(tmp_path / "other-state"), "EXOMEM_VAULT_PATH": str(other),
                            "EXOMEM_WRITER_LEASE_STATE_DIR": str(tmp_path / "lease-other"),
                            "EXOMEM_WRITER_LEASE_REPLICA_ID": "other"}.items():
            there.setenv(name, value)
        service = start_service(other)  # it adopts the replica, which carries no intent
        try:
            call(other, "record_memory", action="append", collection=WORK, item={"title": "Other host row"},
                 why="a store-mode write on another host")
        finally:
            service.stop()


def test_another_host_never_serves_a_store_whose_export_published_its_intent(env, capsys, tmp_path):  # noqa: F811
    """A host that adopts the vault replica mid-export because the replica lacks the export's intent,
    then serves the store and commits a row that the exported files will not hold."""
    from test_collection_store_s1_gate import _until

    root = env
    migrated_and_written(root, capsys)
    crash_export(tmp_path, "before-files", preview_id(root, capsys))  # stopped right after its publication
    client = writer_lease.LeaseCoordinatorClient(writer_lease.LeaseConfig.from_env())
    recorded = client.status().collection_store_head
    other = tmp_path / "other"
    shutil.copytree(root, other)
    with pytest.MonkeyPatch.context() as there:
        for name, value in {"EXOMEM_STATE_ROOT": str(tmp_path / "other-state"), "EXOMEM_VAULT_PATH": str(other),
                            "EXOMEM_WRITER_LEASE_STATE_DIR": str(tmp_path / "lease-other"),
                            "EXOMEM_WRITER_LEASE_REPLICA_ID": "other"}.items():
            there.setenv(name, value)
        service = Service(other, writer_lease.start_server_lifecycle())
        try:
            _until(lambda: service.manager.status().get("collection_store", {}).get("status") in {
                "admitted", "export_pending"}, seconds=30)

            assert service.manager.status()["collection_store"]["status"] == "export_pending"
            with pytest.raises((OpError, ValueError, CollectionStoreError)) as refused:
                call(other, "record_memory", action="append", collection=WORK, item={"title": "Other host row"},
                     why="a store-mode write on another host")
            assert "COLLECTION_STORE_EXPORT_PENDING" in str(refused.value)
        finally:
            with suppress(OpError):
                service.stop()
    assert client.status().collection_store_head == recorded


def test_a_resume_after_another_host_wrote_through_the_replica_refuses_before_it_writes(
        env, capsys, tmp_path):  # noqa: F811
    """A resume that publishes, renews and exports a store behind the coordinator's head: a host
    that adopted the replica before it carried the intent committed a row the files then lack,
    and that row survives only in a store that retire-local later tombstones."""
    root = env
    migrated_and_written(root, capsys)
    plan_id = preview_id(root, capsys)
    crash_export(tmp_path, "after-begin", plan_id)  # the intent is in the live store, not the replica
    another_host_writes(root, tmp_path)
    client = writer_lease.LeaseCoordinatorClient(writer_lease.LeaseConfig.from_env())
    recorded = client.status().collection_store_head
    before = vault_bytes(root)

    assert run_export(root, "--plan-id", plan_id, "--why", "roll back to files") == 1

    assert "COLLECTION_STORE_SYNC_PENDING" in capsys.readouterr().err
    assert client.status().collection_store_head == recorded
    assert vault_bytes(root) == before


def retire_by_hand(store_id):
    """The operator retires the store fence outside the export, as `POST .../collection-store-fence/retire` does."""
    with pytest.MonkeyPatch.context() as shell:
        shell.setenv(OPERATOR, "operator")
        writer_lease.configured_schema_fence_operator_client().retire_collection_store_fence(
            expected_generation=1, store_id=store_id)


def test_an_apply_whose_lease_lapsed_after_its_intent_refuses_behind_another_hosts_head(
        env, capsys, monkeypatch):  # noqa: F811
    """An apply paused past its lease after recording its intent, while another host took the lease,
    adopted the replica and wrote: going on publishes, renews and exports over that host's head, so its
    row survives only in a store that retire-local later tombstones."""
    root = env
    migrated_and_written(root, capsys)
    plan_id = preview_id(root, capsys)
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        store_id = reader.execute("SELECT value FROM store_meta WHERE key='store_id'").fetchone()[0]
        sequence, _ = chain.verify_store_chain(reader)
    elsewhere = writer_lease.LeaseCoordinatorClient(
        replace(writer_lease.LeaseConfig.from_env(), replica_id="another-host"))
    begin = export._begin
    before_rows = canonical_rows(root)
    before_marker, before_fence = authority.read_marker(root), fence()
    continuation = {}

    def pause_while_another_host_writes(*args, **kwargs):
        begun = begin(*args, **kwargs)
        # Successful admission publishes its epoch before this continuation loses authority.
        continuation.update(vault_bytes=vault_bytes(root), intent=begun[0])
        wait_out_the_lease()
        held = elsewhere.acquire(collection_store_capability=writer_lease.COLLECTION_STORE_CAPABILITY)
        # What that host's renewal reports once it committed one row past this store's head.
        elsewhere.renew(held.fencing_token, collection_store_capability=writer_lease.COLLECTION_STORE_CAPABILITY,
                        collection_store_head=writer_lease.CollectionStoreHead(
                            store_id, str(uuid.uuid4()), sequence + 1, "a" * 64))
        continuation["recorded_head"] = elsewhere.status().collection_store_head
        elsewhere.release(held.fencing_token)
        return begun

    monkeypatch.setattr(export, "_begin", pause_while_another_host_writes)

    assert run_export(root, "--plan-id", plan_id, "--why", "roll back to files") == 1

    assert "COLLECTION_STORE_SYNC_PENDING" in capsys.readouterr().err
    assert vault_bytes(root) == continuation["vault_bytes"]
    assert canonical_rows(root) == before_rows
    assert (authority.read_marker(root), fence()) == (before_marker, before_fence)
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        assert authority.pending_create(reader) == continuation["intent"]
        assert authority.pending_create(reader)["plan_id"] == plan_id
    assert elsewhere.status().collection_store_head == continuation["recorded_head"]


def test_a_rerun_after_a_hand_retired_fence_goes_forward_from_the_frozen_store(env, capsys, tmp_path):  # noqa: F811
    """A rerun after a fence retired by hand before the export wrote its files that stays refused with no
    way out, or that retires the marker without writing and verifying the files, so the rows written in
    store mode survive only in the tombstone."""
    root = env
    _, served = migrated_and_written(root, capsys)
    plan_id = preview_id(root, capsys)
    store_id = authority.parse_marker(root, authority.read_marker(root))["store_id"]
    crash_export(tmp_path, "before-files", plan_id)  # the intent is recorded, no file is written
    retire_by_hand(store_id)

    assert run_export(root, "--plan-id", plan_id, "--why", "roll back to files") == 0

    resumed = printed(capsys)
    assert {row["collection_id"]: row["status"] for row in resumed["collections"]} == {
        CID: "written", X3_ID: "written", LEDGER_ID: "unwritten", OTHER: "written"}
    assert authority.read_marker(root) is None and not connection.store_path(root).exists()
    assert (fence().enrolled, fence().generation) == (False, 1)
    assert {cid: rows(root, path) for cid, path in MIGRATED.items()} == served
    assert call(root, "record_memory", action="inspect", collection=WORK)["audit"]["status"] == "acknowledged_gap"


def test_a_hand_retired_fence_over_another_hosts_head_refuses_before_it_writes(env, capsys, tmp_path):  # noqa: F811
    """A rerun that goes forward from a fence retired by hand although another host adopted the replica
    and wrote first: the files would lack that host's row, which then survives only in a store that
    retire-local later tombstones."""
    root = env
    migrated_and_written(root, capsys)
    plan_id = preview_id(root, capsys)
    store_id = authority.parse_marker(root, authority.read_marker(root))["store_id"]
    crash_export(tmp_path, "after-begin", plan_id)  # the intent is in the live store, not the replica
    another_host_writes(root, tmp_path)
    retire_by_hand(store_id)
    before = vault_bytes(root)

    assert run_export(root, "--plan-id", plan_id, "--why", "roll back to files") == 1

    assert "COLLECTION_STORE_SYNC_PENDING" in capsys.readouterr().err
    assert vault_bytes(root) == before
    assert authority.parse_marker(root, authority.read_marker(root))["store_id"] == store_id


def test_an_edit_after_the_exports_own_fence_retire_does_not_stop_its_renames(env, capsys, tmp_path):  # noqa: F811
    """A rerun after the export's own fence retire that reads the files again, so a person's file-mode
    edit made after the store let go refuses it as unverified and leaves the vault routed to a store
    whose fence is gone."""
    root = env
    migrated_and_written(root, capsys)
    plan_id = preview_id(root, capsys)
    crash_export(tmp_path, "after-fence-retire", plan_id)
    view = root / WORK.replace("_collection.md", "Items/Original.md")
    edited = view.read_bytes() + b"\nA note added in file mode\n"
    view.write_bytes(edited)

    assert run_export(root, "--plan-id", plan_id, "--why", "roll back to files") == 0

    assert printed(capsys)["resumed"] == "after the fence retire"
    assert authority.read_marker(root) is None and not connection.store_path(root).exists()
    assert view.read_bytes() == edited


def test_an_export_that_cannot_finish_is_abandoned_keeping_every_row_and_the_edit(env, capsys, tmp_path):  # noqa: F811
    """An export stuck behind a view a person edited whose only way out loses a store row, adopts or
    overwrites the edit, or orphans the checkpoints it already published, so that every later export
    reads a transition fork and ends unverified."""
    root = env
    _, served = migrated_and_written(root, capsys)
    plan_id = preview_id(root, capsys)
    crash_export(tmp_path, "between-work-files-and-checkpoint", plan_id)
    view = root / WORK.replace("_collection.md", "Items/Original.md")
    original = view.read_bytes()
    edited = original + b"\nA note added by hand\n"
    view.write_bytes(edited)
    assert run_export(root, "--plan-id", plan_id, "--why", "roll back to files") == 1
    assert export.VIEW_EDITED in capsys.readouterr().err
    before = vault_bytes(root, outside=COLLECTIONS)

    assert run_export(root, "--abandon", "--plan-id", plan_id, "--why", "the edit needs a decision first") == 0

    abandoned = printed(capsys)
    assert (abandoned["status"], abandoned["checkpoints"]) == ("abandoned", sorted([OTHER, X3_ID]))
    assert vault_bytes(root, outside=COLLECTIONS) == before
    service = start_service(root)
    try:
        assert {cid: rows(root, path) for cid, path in MIGRATED.items()} == served
    finally:
        service.stop()
    assert view.read_bytes() == edited  # kept where the person wrote it, and adopted nowhere
    view.write_bytes(original)  # the owner decides against the note

    assert run_export(root, "--plan-id", preview_id(root, capsys), "--why", "roll back to files") == 0

    assert {row["collection_id"]: row["audit"] for row in printed(capsys)["collections"]} == {
        CID: "acknowledged_gap", X3_ID: "acknowledged_gap", LEDGER_ID: "ok", OTHER: "acknowledged_gap"}
    assert {cid: rows(root, path) for cid, path in MIGRATED.items()} == served


def test_an_abandon_after_a_checkpoint_torn_between_manifest_and_log_exports_again(env, capsys, tmp_path):  # noqa: F811
    """An abandon that takes a checkpoint as the next export's parent although only the manifest
    flip of its write landed and the activity log never received it, so every later export
    continues a chain from an event that does not exist and ends unverified."""
    root = env
    _, served = migrated_and_written(root, capsys)
    plan_id = preview_id(root, capsys)
    crash_export(tmp_path, "between-work-files-and-checkpoint", plan_id)
    # The checkpoint write flips the manifest, then the log; a kill between them leaves this.
    torn = export._transition(plan_id, OTHER)
    log = root / "Knowledge Base/log.md"
    log.write_text("".join(line for line in log.read_text().splitlines(keepends=True) if torn not in line))

    assert run_export(root, "--abandon", "--plan-id", plan_id, "--why", "the export cannot finish") == 0
    abandoned = printed(capsys)

    assert run_export(root, "--plan-id", preview_id(root, capsys), "--why", "roll back to files") == 0
    assert {row["collection_id"]: row["audit"] for row in printed(capsys)["collections"]} == {
        CID: "acknowledged_gap", X3_ID: "acknowledged_gap", LEDGER_ID: "ok", OTHER: "acknowledged_gap"}
    assert {cid: rows(root, path) for cid, path in MIGRATED.items()} == served
    assert abandoned["checkpoints"] == [X3_ID]


def test_an_abandon_cut_after_its_transaction_leaves_a_store_the_service_serves(env, capsys, tmp_path):  # noqa: F811
    """An abandon killed between its store transaction and its publication that leaves the store
    unadmittable, or still owned by the export it gave up."""
    root = env
    _, served = migrated_and_written(root, capsys)
    plan_id = preview_id(root, capsys)
    crash_export(tmp_path, "between-files-and-checkpoint", plan_id)
    crash_export(tmp_path, "after-abandon-transaction", plan_id, "--abandon")

    service = start_service(root)
    try:
        assert {cid: rows(root, path) for cid, path in MIGRATED.items()} == served
        written = call(root, "record_memory", action="append", collection=WORK, item={"title": "After abandon"},
                       why="a store write once the export was abandoned")
        assert written["outcome"] == "committed"
    finally:
        service.stop()
    assert run_export(root, "--dry-run", operator=False) == 0 and printed(capsys)["verdict"] == "exportable"


def test_a_host_that_lost_its_store_after_the_fence_retire_finishes_with_renames_and_retire_local(
        env, capsys, tmp_path):  # noqa: F811
    """The documented recovery for an exporting host whose live store is lost after the fence
    retire: retire-local that keeps the store's descriptors because no store is left, so a
    release from before the store still refuses this state."""
    root = env
    _, served = migrated_and_written(root, capsys)
    crash_export(tmp_path, "after-fence-retire", preview_id(root, capsys))
    with closing(connection.open_head_reader(connection.store_path(root))) as reader:
        at = authority.pending_create(reader)["exported_at"]
    for path in state_paths.vault_state_dir(root).glob("collections*.sqlite*"):
        path.unlink()
    (root / COLLECTIONS / "mode.json").rename(root / COLLECTIONS / f"mode.json.exported-{at}")
    (root / COLLECTIONS / "collections.sqlite").rename(root / COLLECTIONS / f"collections.sqlite.exported-{at}")

    assert run_export(root, "--retire-local", operator=False) == 0

    assert printed(capsys)["status"] == "retired"
    assert not {"collections-store-v1", state_migration.COLLECTION_MARKER_COMPATIBILITY_ID} & set(
        state_migration.recorded_descriptor_ids(root))
    assert {cid: rows(root, path) for cid, path in MIGRATED.items()} == served


def test_the_retire_probe_needs_the_operator_credential_and_moves_no_fence(env, capsys, monkeypatch):  # noqa: F811
    """A probe that passes on a credential the coordinator rejects, so an export writes every
    file and then cannot retire the fence, or a probe that changes the fence it checks."""
    root = env
    legacy_vault(root)
    assert migrate(root, "apply") == 0 and printed(capsys)["status"] == "marker_admitted"
    enrolled = fence()
    monkeypatch.setenv(OPERATOR, "operator")
    writer_lease.configured_schema_fence_operator_client().probe_collection_store_fence_retire()
    monkeypatch.setenv(OPERATOR, "not-the-operator")

    with pytest.raises(OpError) as refused:
        writer_lease.configured_schema_fence_operator_client().probe_collection_store_fence_retire()

    assert refused.value.code == "SCHEMA_FENCE_OPERATOR_UNAVAILABLE"
    assert fence() == enrolled and enrolled.enrolled


def test_an_export_refuses_before_it_writes_on_a_coordinator_that_cannot_read_a_retired_fence(
        env, capsys, monkeypatch):  # noqa: F811
    """An export on a coordinator release that retires a store fence but cannot read the retired
    fence back, so it writes every file and retires the fence, and a rerun after a cut there can
    never check the head the fence was retired at, so never goes forward."""
    root = env
    migrated_and_written(root, capsys)
    plan_id = preview_id(root, capsys)
    store_id = authority.parse_marker(root, authority.read_marker(root))["store_id"]
    before = vault_bytes(root)

    def route_absent(client):
        # What the client raises when the coordinator answers this route with 404.
        raise writer_lease._contract_absent_error(
            client.config.url, "/v1/vaults/<id>/collection-store-fence/retired", 404)

    monkeypatch.setattr(writer_lease.LeaseCoordinatorClient, "retired_collection_store_fence", route_absent)

    assert run_export(root, "--plan-id", plan_id, "--why", "roll back to files") == 1

    assert "COLLECTION_STORE_COORDINATOR_UPGRADE_REQUIRED" in capsys.readouterr().err
    nothing_changed(root, before, store_id)


def test_a_log_over_the_file_mode_bound_keeps_the_vault_in_the_store(env, capsys, monkeypatch):  # noqa: F811
    """An export that writes a log file mode then refuses to read, or that names the log's
    bound as a value with no file form, so the owner cannot tell what to shrink."""
    root = env
    migrated_and_written(root, capsys)
    store_id = authority.parse_marker(root, authority.read_marker(root))["store_id"]
    before = vault_bytes(root)
    monkeypatch.setattr(record_formats, "_MAX_LOG_BYTES", 64)

    assert run_export(root, "--dry-run", operator=False) == 1

    previewed = printed(capsys)
    assert previewed["verdict"] == "blocked" and "plan_id" not in previewed
    assert {row["collection_id"]: row["blocker"]["code"] for row in previewed["collections"]
            if row["status"] == "blocked"} == {X3_ID: export.OVER_BOUND}
    nothing_changed(root, before, store_id)


def nothing_changed(root, before, store_id):
    """The vault after a refused apply: still routed to the same store, fenced, its files untouched."""
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        assert reader.execute("SELECT 1 FROM store_meta WHERE key=?", (authority.PENDING_CREATE,)).fetchone() is None
    assert authority.parse_marker(root, authority.read_marker(root))["store_id"] == store_id
    assert (fence().enrolled, fence().store_id) == (True, store_id)
    assert vault_bytes(root) == before


def test_a_view_edited_by_hand_blocks_the_export_and_names_its_path(env, capsys):  # noqa: F811
    """An export that overwrites a person's edit to a store view, which P2 has not classified yet."""
    root = env
    migrated_and_written(root, capsys)
    plan_id = preview_id(root, capsys)
    view = root / WORK.replace("_collection.md", "Items/Original.md")
    view.write_text(view.read_text() + "\nA note added by hand\n")
    store_id = authority.parse_marker(root, authority.read_marker(root))["store_id"]
    before = vault_bytes(root)

    assert run_export(root, "--dry-run", operator=False) == 1
    blocked = {row["path"]: row for row in printed(capsys)["collections"] if row["status"] == "blocked"}
    assert run_export(root, "--plan-id", plan_id, "--why", "roll back to files") == 1

    assert list(blocked) == [WORK] and blocked[WORK]["blocker"]["code"] == export.VIEW_EDITED
    assert view.relative_to(root).as_posix() in blocked[WORK]["blocker"]["reason"]
    assert export.VIEW_EDITED in capsys.readouterr().err
    nothing_changed(root, before, store_id)


def test_a_store_write_after_the_preview_makes_its_plan_stale(env, capsys):  # noqa: F811
    """An apply that exports a plan older than the store, losing the write made after its preview."""
    root = env
    migrated_and_written(root, capsys)
    plan_id = preview_id(root, capsys)
    service = start_service(root)
    try:
        call(root, "record_memory", action="append", collection=WORK, item={"title": "After preview"},
             why="a write after the preview")
    finally:
        service.stop()
    store_id = authority.parse_marker(root, authority.read_marker(root))["store_id"]
    before = vault_bytes(root)

    assert run_export(root, "--plan-id", plan_id, "--why", "roll back to files") == 1

    assert export.STALE in capsys.readouterr().err
    nothing_changed(root, before, store_id)


def test_a_collection_the_store_created_keeps_the_whole_vault_in_the_store(s1, capsys):  # noqa: F811
    """An export that drops a store-native summary collection, which file mode never held, or that
    exports the rest and leaves that one behind a retired fence (the current conservative rule)."""
    root = s1
    assert migrate(root, "apply") == 0 and printed(capsys)["status"] == "marker_admitted"
    store_id = authority.parse_marker(root, authority.read_marker(root))["store_id"]
    before = vault_bytes(root)

    assert run_export(root, "--dry-run", operator=False) == 1
    previewed = printed(capsys)

    blocked = {row["collection_id"]: row["blocker"]["code"] for row in previewed["collections"]
               if row["status"] == "blocked"}
    assert previewed["verdict"] == "blocked" and "plan_id" not in previewed
    assert blocked == {DAILY: export.STORE_NATIVE}
    nothing_changed(root, before, store_id)


def test_an_apply_refuses_while_a_service_holds_the_vault(env, capsys):  # noqa: F811
    """An apply that takes the lease beside a service another host runs, or a service whose
    maintain_memory applies the export itself and so retires the fence it writes under."""
    root = env
    migrated_and_written(root, capsys)
    plan_id = preview_id(root, capsys)
    store_id = authority.parse_marker(root, authority.read_marker(root))["store_id"]
    before = vault_bytes(root)
    before_rows = canonical_rows(root)
    before_marker, before_fence = authority.read_marker(root), fence()
    elsewhere = writer_lease.LeaseCoordinatorClient(
        replace(writer_lease.LeaseConfig.from_env(), replica_id="another-host"))
    held = elsewhere.acquire(collection_store_capability=writer_lease.COLLECTION_STORE_CAPABILITY)
    assert held.holder == "another-host"

    assert run_export(root, "--plan-id", plan_id, "--why", "roll back to files") == 1

    assert "COLLECTION_STORE_SERVICE_ACTIVE" in capsys.readouterr().err
    nothing_changed(root, before, store_id)
    elsewhere.release(held.fencing_token)
    service = start_service(root)
    try:
        # Startup publishes its new epoch independently of either refused export.
        before = vault_bytes(root)
        assert canonical_rows(root) == before_rows
        with pytest.raises((OpError, ValueError, CollectionStoreError)) as refused:
            call(root, "maintain_memory", mode="collections-store-export", apply=True, plan_id=plan_id,
                 why="roll back to files")
        assert "COLLECTION_STORE_SERVICE_ACTIVE" in str(refused.value)
    finally:
        service.stop()
    nothing_changed(root, before, store_id)
    assert canonical_rows(root) == before_rows
    assert (authority.read_marker(root), fence()) == (before_marker, before_fence)


def test_a_vault_migrates_again_after_its_export_at_the_next_fence_generation(env, capsys):  # noqa: F811
    """A re-migration that the retired fence or the tombstones block, that reuses the exported store's
    identity or generation, or that serves rows other than the exported files hold."""
    root = env
    old = exported(root, capsys)["store_id"]
    files = {cid: rows(root, path) for cid, path in MIGRATED.items()}

    assert migrate(root, "apply") == 0
    applied = printed(capsys)

    routed = authority.parse_marker(root, authority.read_marker(root))
    assert applied["status"] == "marker_admitted" and routed["store_id"] != old
    assert routed["collection_store_fence"]["generation"] == 2
    assert (fence().enrolled, fence().generation, fence().store_id) == (True, 2, routed["store_id"])
    service = start_service(root)
    try:
        assert {cid: rows(root, path) for cid, path in MIGRATED.items()} == files
    finally:
        service.stop()
