"""The installed service serving the S1 collection store.

OpenSpec add-collection-query-engine S1.8 and the structured-collections scenarios for
coexisting A/B/C, restart and slice rollback. Each case drives what production runs:
the owner enrols C offline with ``exomem collections create`` and the operator
credential, ``start_server_lifecycle`` starts the service without that credential, and
``invoke_command`` is the dispatcher MCP, REST and the CLI share. No case binds a writer
or sets the preview flag, and records-summary-v1 is turned on only in this temporary
state, as the release would.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import functools
import json
import os
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_collection_store_importer import ndjson, write_source
from test_collection_store_s1_gate import (
    A_PATH,
    B_PATH,
    KEY,
    LATER,
    OTHER,
    ROW,
    _preserved,
    _published,
    _until,
    ab_bytes,
    coordinator,
    run_host,
)
from test_collection_store_summary import summary_text
from test_collection_store_writer import CID, manifest_path, manifest_text
from test_typed_history_generations import previous_release

from exomem import __main__ as cli
from exomem import (
    capabilities,
    command_surface,
    commands,
    record_governance,
    records,
    reserved_paths,
    state_migration,
    vault,
    writer_lease,
)
from exomem import structured_collections as collections
from exomem.cli_ops import OpError
from exomem.collection_store import (
    admission,
    authority,
    capability,
    connection,
    custody,
    importer,
    replica,
    runtime,
    schema,
)
from exomem.collection_store.connection import CollectionStoreError
from exomem.governance import egress
from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope
from exomem.lease_coordinator import SQLiteLeaseStore

OWNER = owner_principal(surface="mcp")
DAILY = "66666666-6666-4666-8666-666666666666"
DAILY_PATH = manifest_path().replace("Work", "Daily")
SOURCE = "Knowledge Base/Evidence/service/export/rows.ndjson"
MAPPING = {"fields": {"title": "title", "count": "count"}}
ON = frozenset({capability.RECORDS_SUMMARY_V1})
OPERATOR = "EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN"
DAILY_TEXT = summary_text(manifest_text().replace(CID, DAILY).replace("title: Work", "title: Daily"))


def call(root, tool, **arguments):
    command = next(command for command in commands.product_commands_for("mcp") if command.name == tool)
    with request_scope(OWNER):
        return writer_lease.invoke_command(command, root, **arguments)


def imported(root, collection=CID, **request):
    return call(root, "record_memory", action="import", collection=collection, import_request=request)


def titles(root, collection):
    return sorted(row["title"] for row in call(root, "record_memory", action="query", collection=collection)["rows"])


def journal_rows(tmp_path):
    path = tmp_path / "logs" / "mutations.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def busy_total():
    from exomem import metrics

    return sum(counter["value"] for counter in metrics.snapshot()["counters"]
               if counter["name"] == "exomem_mutation_busy_total")


def store_meta(root):
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        return dict(reader.execute("SELECT key,value FROM store_meta"))


def refused(work, code=capability.UNAVAILABLE):
    with pytest.raises((OpError, collections.CollectionError, CollectionStoreError)) as error:
        work()
    assert error.value.code == code, error.value


def test_a_withheld_selection_without_a_service_is_indistinguishable_from_missing(service):
    """Routing must not disclose marker ownership before collection visibility is checked."""
    from test_collection_field_admission import GUEST
    from test_governance_egress import write_rule, write_scope

    root = service.root
    service.stop()
    write_scope(root, paths="Records/**")
    write_rule(root, ceiling=0)
    command = next(command for command in commands.product_commands_for("mcp") if command.name == "record_memory")
    before = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*.md")}
    errors = []
    with request_scope(GUEST):
        for selector in (CID, "99999999-9999-4999-8999-999999999999"):
            with pytest.raises((OpError, collections.CollectionError, CollectionStoreError)) as error:
                writer_lease.invoke_command(command, root, action="append", collection=selector,
                                            item={"title": "Proposal"}, why="capture")
            errors.append((error.value.code, error.value.message if isinstance(error.value, OpError) else error.value.reason))
    assert errors[0] == errors[1] and errors[0][0] == "COLLECTION_NOT_FOUND"
    refused(lambda: call(root, "record_memory", action="append", collection=CID,
                         item={"title": "Proposal"}, why="capture"), "COLLECTION_STORE_UNAVAILABLE")
    assert {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*.md")} == before


@dataclass
class Service:
    root: Path
    manager: writer_lease.LeaseManager

    def stop(self, *, deadline=None):
        server = runtime._SERVERS.get(self.root)
        try:
            if deadline is None:
                self.manager.close()
            else:
                self.manager.close(deadline=deadline)
        finally:
            if server is not None:
                server.thread.join(10)
            writer_lease._MANAGERS.pop(self.manager.config, None)


def offline_create(root, manifest_file, path, *, why):
    """The owner's `exomem collections create`, with the operator credential in its own shell."""
    with pytest.MonkeyPatch.context() as shell:
        shell.setenv(OPERATOR, "operator")
        return cli._collections_main(["create", "--vault", str(root), "--manifest-path", path,
                                      "--manifest-file", str(manifest_file), "--why", why])


def _served_vault(tmp_path, monkeypatch):
    """A/B in files and the service's environment, before the owner enrols summary C."""
    root = tmp_path / "vault"
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv(OPERATOR, raising=False)
    monkeypatch.delenv(custody.SYNC_ROOTS_ENV, raising=False)
    monkeypatch.setattr("exomem.records._capture_sweep_carrier", lambda *a, **kw: None)
    monkeypatch.setattr("exomem.records._due_state_carrier", lambda *a, **kw: None)
    (root / "Knowledge Base/_Schema").mkdir(parents=True)
    (root / "Knowledge Base/_Schema/SKILL.md").write_text("# Schema\n")  # what makes it a vault to the service
    (root / "Knowledge Base/log.md").write_text("# Activity\n")
    state_migration.require_vault_state_ready(root)  # a vault the service has already served: its state is ready
    for path, profile, cid in ((A_PATH, "records", KEY), (B_PATH, "planning", OTHER)):
        records.create_collection(root, path, manifest_text(profile).replace(CID, cid),
                                  why="file fixture", scaffold=True)
    for name, value in {
        "EXOMEM_VAULT_PATH": str(root), "EXOMEM_WRITER_LEASE_URL": "http://localhost",
        "EXOMEM_WRITER_LEASE_VAULT_ID": "gate", "EXOMEM_WRITER_LEASE_REPLICA_ID": "service",
        "EXOMEM_WRITER_LEASE_TOKEN": "lease", "EXOMEM_WRITER_LEASE_STATE_DIR": str(tmp_path / "lease-service"),
        "EXOMEM_WRITER_LEASE_PREFERRED": "1",
    }.items():
        monkeypatch.setenv(name, value)
    (tmp_path / "c.md").write_text(summary_text())
    # Only a release that enables the slice enrols a vault and serves its summary collection C.
    monkeypatch.setattr(capability, "RELEASED", ON)
    return root


@pytest.fixture
def service(tmp_path, monkeypatch):
    """A/B in files, summary C enrolled offline by the owner, then the service started without the operator
    credential, all under a release that enables records-summary-v1."""
    root = _served_vault(tmp_path, monkeypatch)
    with coordinator(tmp_path / "coordinator.sqlite"):
        assert offline_create(root, tmp_path / "c.md", manifest_path(), why="new store collection") == 0
        started = Service(root, writer_lease.start_server_lifecycle())
        try:
            _until(lambda: started.manager.status().get("collection_store", {}).get("status") == "admitted")
            assert call(root, "record_memory", action="append", collection=CID, item={"title": "Canonical"},
                        item_key=ROW, why="first row")["outcome"] == "committed"
            yield started
        finally:
            if not started.manager._stop.is_set():
                started.stop()


def test_an_upgraded_service_migrates_its_fenced_store_from_the_previous_schema_and_stays_writable(
        tmp_path, monkeypatch):
    """A release that bumps the store schema must let its writer migrate a store the previous release fenced.

    Readers refuse a store below the current schema until its writer migrates it, so a writer that
    needs a reader to attach the store never acquires the fenced lease, and every write refuses.
    """
    root = _served_vault(tmp_path, monkeypatch)
    with coordinator(tmp_path / "coordinator.sqlite"):
        with monkeypatch.context() as previous:
            previous_release(previous)
            assert offline_create(root, tmp_path / "c.md", manifest_path(), why="new store collection") == 0
        with closing(sqlite3.connect(connection.store_path(root))) as raw:
            assert schema.schema_version(raw) == 10
        # The previous release's worker still holds the lease while the upgraded one starts, as in a handoff.
        previous_worker = writer_lease.LeaseCoordinatorClient(writer_lease.LeaseConfig.from_env(
            {**os.environ, "EXOMEM_WRITER_LEASE_REPLICA_ID": "previous-worker"}))
        held = previous_worker.acquire(collection_store_capability=writer_lease.COLLECTION_STORE_CAPABILITY)
        assert held.granted
        started = Service(root, writer_lease.start_server_lifecycle())
        try:
            _until(lambda: runtime.served(root))
            refused(lambda: call(root, "record_memory", action="append", collection=CID,
                                 item={"title": "During handoff"}, why="the previous worker still writes"),
                    "WRITER_LEASE_REQUIRED")
            with closing(sqlite3.connect(connection.store_path(root))) as raw:
                assert schema.schema_version(raw) == 10  # only the lease holder migrates
            previous_worker.release(held.fencing_token)
            assert call(root, "record_memory", action="append", collection=CID, item={"title": "After upgrade"},
                        item_key=ROW, why="first row after the upgrade")["outcome"] == "committed"
            assert titles(root, CID) == ["After upgrade"]
            assert call(root, "record_memory", action="append", collection=KEY, item={"title": "File row"},
                        why="file collection after the upgrade")["outcome"] == "committed"
        finally:
            started.stop()
    with closing(sqlite3.connect(connection.store_path(root))) as raw:
        assert schema.schema_version(raw) == schema.SCHEMA_VERSION


def test_first_offline_collection_creation_preserves_trashed_declarations(tmp_path, monkeypatch):
    """A recoverable declaration in trash cannot block enrollment of a new store."""
    root = tmp_path / "vault"
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv(custody.SYNC_ROOTS_ENV, raising=False)
    (root / "Knowledge Base/_Schema").mkdir(parents=True)
    (root / "Knowledge Base/_Schema/SKILL.md").write_text("# Schema\n")
    state_migration.require_vault_state_ready(root)
    trashed = root / "Knowledge Base/_trash/2026-01-01/removed/_collection.md"
    trashed.parent.mkdir(parents=True)
    original = summary_text().encode()
    trashed.write_bytes(original)
    source = tmp_path / "manifest.md"
    source.write_bytes(original)
    for name, value in {
        "EXOMEM_VAULT_PATH": str(root), "EXOMEM_WRITER_LEASE_URL": "http://localhost",
        "EXOMEM_WRITER_LEASE_VAULT_ID": "gate", "EXOMEM_WRITER_LEASE_REPLICA_ID": "service",
        "EXOMEM_WRITER_LEASE_TOKEN": "lease", "EXOMEM_WRITER_LEASE_STATE_DIR": str(tmp_path / "lease"),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(capability, "RELEASED", ON)
    with coordinator(tmp_path / "coordinator.sqlite"):
        assert offline_create(root, source, manifest_path(), why="first store collection") == 0
    marker = authority.parse_marker(root, authority.read_marker(root))
    assert [entry["collection_id"] for entry in marker["collections"]] == [CID]
    assert trashed.read_bytes() == original


def test_an_offline_create_after_a_retire_enrols_the_next_fence_generation(tmp_path, monkeypatch):
    """Defect: a vault whose earlier store fence was retired can never move to the store again,
    because its first create cuts from generation 0 and refuses the retired generation."""
    root = tmp_path / "vault"
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv(custody.SYNC_ROOTS_ENV, raising=False)
    (root / "Knowledge Base/_Schema").mkdir(parents=True)
    (root / "Knowledge Base/_Schema/SKILL.md").write_text("# Schema\n")
    state_migration.require_vault_state_ready(root)
    source = tmp_path / "manifest.md"
    source.write_text(summary_text())
    for name, value in {
        "EXOMEM_VAULT_PATH": str(root), "EXOMEM_WRITER_LEASE_URL": "http://localhost",
        "EXOMEM_WRITER_LEASE_VAULT_ID": "gate", "EXOMEM_WRITER_LEASE_REPLICA_ID": "service",
        "EXOMEM_WRITER_LEASE_TOKEN": "lease", "EXOMEM_WRITER_LEASE_STATE_DIR": str(tmp_path / "lease"),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(capability, "RELEASED", ON)
    # The vault's earlier store was enrolled at generation 1, then retired when it went back to files.
    leases = SQLiteLeaseStore(tmp_path / "coordinator.sqlite")
    earlier = {"capability": "collections-store-v1", "store_id": "77777777-7777-4777-8777-777777777777"}
    leases.transition_collection_store_fence("gate", expected_generation=0, **earlier)
    leases.retire_collection_store_fence("gate", expected_generation=1, **earlier)
    with coordinator(tmp_path / "coordinator.sqlite"):
        assert offline_create(root, source, manifest_path(), why="back to the store") == 0
    marker = authority.parse_marker(root, authority.read_marker(root))
    assert marker["collection_store_fence"]["generation"] == 2
    assert leases.collection_store_fence("gate") == {
        "capability": "collections-store-v1", "enrolled": True, "store_id": marker["store_id"], "generation": 2}


def test_nested_archive_declarations_retain_file_source_ownership(tmp_path):
    """A live declaration in a nested archive still owns its source outside that directory."""
    root = tmp_path / "vault"
    path = "Knowledge Base/Records/_archive/Declared/_collection.md"
    source = "Knowledge Base/Records/Work/Items"
    manifest = root / path
    manifest.parent.mkdir(parents=True)
    manifest.write_text(manifest_text().replace("source: Items", "source: " + source))
    assert collections.load_manifest(root, path).storage.source == source
    proposed = {"manifest_path": DAILY_PATH, "source_path": source, "layout": "markdown-items"}
    refused(lambda: authority.require_new_file_ownership(root, None, proposed),
            "COLLECTION_STORE_MARKER_CONFLICT")


def test_the_service_startup_gate_admits_the_mixed_vault(service):
    """Defect: the installed service refuses to start on a vault whose marker routes C to the store."""
    state_migration.require_vault_state_ready(service.root)


@pytest.mark.parametrize("include_history,rollovers", [(False, 1), (True, 1), (False, 2)])
def test_query_reopens_after_the_store_handle_changes_before_mcp_egress(
        service, monkeypatch, include_history, rollovers):
    """Idle handoff must reread a query instead of hiding an unchanged collection."""
    command = next(command for command in commands.COMMANDS if command.name == "record_memory")
    bound = command_surface.bind_vault(command.leaf, service.root, command=command)
    server_runtime = runtime._SERVERS[service.root].runtime
    invoke = writer_lease.invoke_command
    calls = 0

    def handoff_after_read(*args, **kwargs):
        nonlocal calls
        result = invoke(*args, **kwargs)
        calls += 1
        if calls <= rollovers:
            assert server_runtime.retire_idle_handle()
        return result

    monkeypatch.setattr(writer_lease, "invoke_command", handoff_after_read)
    with request_scope(OWNER):
        result = bound(action="query", collection=CID, include_agent_history=include_history)

    assert calls == 2
    if rollovers == 2:
        assert result["error"]["code"] == "COLLECTION_STORE_BUSY"
        assert "rows" not in result
    else:
        assert [row["title"] for row in result["rows"]] == ["Canonical"]
        if include_history:
            assert result["agent_history"]["events"]


def test_a_busy_consistency_read_does_not_cost_a_publication_interval(tmp_path, monkeypatch, caplog):
    """A brief read must not delay an unstarted replica publication for a full minute."""
    root = _served_vault(tmp_path, monkeypatch)
    caplog.set_level("DEBUG", logger="exomem.mutation_lock")
    with coordinator(tmp_path / "coordinator.sqlite"):
        assert offline_create(root, tmp_path / "c.md", manifest_path(), why="store fixture") == 0
        with monkeypatch.context() as timing:
            # Keep scheduling out of setup; restore the normal timers while the read holds the boundary.
            timing.setattr(runtime, "PUBLISH_INTERVAL_SECONDS", 3600)
            timing.setattr(runtime, "PUBLISH_SETTLE_SECONDS", 3600)
            started = Service(root, writer_lease.start_server_lifecycle())
            try:
                _until(lambda: started.manager.status().get("collection_store", {}).get("status") == "admitted")
                assert call(root, "record_memory", action="append", collection=CID,
                            item={"title": "Published after the read"}, item_key=LATER,
                            why="publication fixture")["outcome"] == "committed"

                def views_ready():
                    with closing(connection.open_reader(connection.store_path(root))) as reader:
                        return reader.execute(
                            "SELECT 1 FROM projection_state WHERE state='pending' LIMIT 1"
                        ).fetchone() is None

                _until(views_ready)
                with started.manager.consistency_guard(root, operation="publication-test-read"):
                    timing.undo()
                    # Synchronize on a real publisher refusal; durable replica data is the outcome below.
                    _until(lambda: any(
                        getattr(record, "event", None) == "mutation_lock_refused"
                        and record.threadName == "exomem-collection-replica"
                        and getattr(record, "fields", {}).get("operation") == "publication-test-read"
                        for record in caplog.records
                    ))
                _published(SimpleNamespace(meta=lambda: store_meta(root)))
                with closing(connection.open_reader(replica.replica_path(root))) as reader:
                    assert reader.execute(
                        "SELECT COUNT(*) FROM items WHERE item_key=?", (LATER,)
                    ).fetchone() == (1,)
            finally:
                timing.undo()
                started.stop()


def test_the_service_serves_c_from_its_own_session_and_keeps_a_and_b_in_files(service, monkeypatch):
    """Defect: the installed service never opens its store, so C refuses or reads its views as
    files; A/B rows reach the store or C traffic changes A/B bytes; or the in-service owner
    route refuses SERVICE_ACTIVE or drops the owner's acknowledgement of skipped changes; or an
    inventory lists C's generated view as a file collection."""
    root = service.root
    # A slow runner can publish the fixture's row before the next write; the next write then waits out
    # the 60 s window and the publisher never settles inside the test's wait.
    monkeypatch.setattr(runtime, "PUBLISH_INTERVAL_SECONDS", 0.1)
    assert call(root, "record_memory", action="append", collection=KEY, item={"title": "File row"},
                item_key=ROW, why="file write")["outcome"] == "committed"
    before = ab_bytes(root)
    assert call(root, "record_memory", action="append", collection=CID, item={"title": "Through the service"},
                item_key=LATER, why="service write")["outcome"] == "committed"
    assert titles(root, CID) == ["Canonical", "Through the service"]
    assert titles(root, KEY) == ["File row"]
    assert ab_bytes(root) == before
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        assert reader.execute("SELECT collection_id,COUNT(*) FROM items GROUP BY 1").fetchall() == [(CID, 2)]
    inspected = call(root, "record_memory", action="inspect", collection=CID)
    # The MCP layer's second egress pass runs on its own thread, as this test does,
    # and serves the inspection only to the principal the store admitted it for.
    with request_scope(OWNER):
        assert egress.postfilter("record_memory", inspected, root) == inspected
    with request_scope(RequestPrincipal(audience_id="external-agent", surface="mcp")):
        refused(lambda: egress.postfilter("record_memory", inspected, root), "COLLECTION_NOT_FOUND")
    inventory = call(root, "record_memory", action="inspect")
    assert {row["collection_id"]: row["committed"] for row in inventory["collections"]} == {KEY: 1, CID: 2}
    assert inventory["unreadable_manifests"] == []
    with request_scope(OWNER):  # no store bound, as in a process that does not serve the vault
        unserved = record_governance.inventory_collections(root)
        discovered, _ = collections.discover_collections_with_errors(root)
    assert CID not in {manifest.collection_id for manifest in discovered}
    assert [row["collection_id"] for row in unserved["collections"]] == [KEY]
    assert [(row["path"], row["error_code"]) for row in unserved["unreadable_manifests"]] == [
        (manifest_path(), "COLLECTION_STORE_UNAVAILABLE")]
    _published(SimpleNamespace(meta=lambda: store_meta(root)))  # the off-ack publisher settles first
    preview = call(root, "maintain_memory", mode="collections-store-adopt-local")
    # The acknowledgement reaches the route, which keeps it for the reconcile step.
    refused(lambda: call(root, "maintain_memory", mode="collections-store-adopt-local", apply=True,
                         plan_id=preview["plan_id"], why="acknowledge", acknowledge_skipped=True),
            "COLLECTION_STORE_ACKNOWLEDGE_UNAVAILABLE")
    assert call(root, "maintain_memory", mode="collections-store-adopt-local", apply=True,
                plan_id=preview["plan_id"], why="nothing to adopt")["status"] == "in_sync"


def test_structured_files_maintenance_reaches_a_store_collection_through_the_service(service):
    """Defect: maintain_memory structured-files on a store collection runs unbound in the service,
    falls back to file collections and answers COLLECTION_NOT_FOUND."""
    # The store writer answers for its collection: summary C keeps one aggregate view, not one per item.
    with pytest.raises(collections.CollectionError, match="UNSUPPORTED_STORAGE"):
        call(service.root, "maintain_memory", mode="structured-files", collection=CID)
    with pytest.raises(collections.CollectionError, match="UNSUPPORTED_STORAGE"):
        call(service.root, "maintain_memory", mode="structured-files", collection=CID, apply=True,
             plan_id="0" * 64, source_snapshot="0" * 64, why="apply reaches the store writer")


def test_a_remote_adopt_local_apply_succeeds_while_the_service_runs_and_a_remote_export_apply_does_not(service):
    """Defect: a remote client cannot apply the reviewed adopt-local plan to the service it talks to
    and is sent to the host, where the CLI refuses while the service runs; or a remote export apply
    stops short of the host and reaches the offline-only route."""
    root = service.root
    descriptor = capabilities.ActiveSurfaceDescriptor(
        surface="mcp", profile="test", tier2_enabled=True, product_commands=("maintain_memory",)
    )

    def remote(**arguments):
        with capabilities.active_surface(descriptor):
            return call(root, "maintain_memory", **arguments)

    found = SimpleNamespace(root=root, meta=lambda: store_meta(root))
    _published(found)
    path = replica.replica_path(root)
    path.write_bytes(path.read_bytes() + b"\0")  # another copy published over this host's replica
    _preserved(found)
    preview = remote(mode="collections-store-adopt-local")
    applied = remote(mode="collections-store-adopt-local", apply=True, plan_id=preview["plan_id"],
                     why="adopt over the remote surface")

    assert (preview["step"], applied["status"]) == ("adopt-local", "adopted"), applied.get("reason")
    refused(lambda: remote(mode="collections-store-export", apply=True, plan_id="reviewed", why="export"),
            "MAINTENANCE_REQUIRES_CLI")


def test_records_summary_v1_gates_summary_create_import_and_query_until_released(
        service, tmp_path, monkeypatch, capsys):
    """Defect: the service creates, imports into or answers a summary collection before the release
    enables records-summary-v1, a disabled summary route answers as empty, the gate also stops the
    file collections, an unenrolled vault's summary create needs no operator step, the offline
    create runs beside the service or enrols an items-mode collection in the store, or the offline
    create enrols a vault the release keeps dark."""
    root = service.root
    monkeypatch.setattr(capability, "RELEASED", frozenset())
    write_source(root, ndjson({"title": f"Row {i}", "count": i} for i in range(3)), SOURCE)
    marker = authority.read_marker(root)
    unreleased = tmp_path / "unreleased"
    (unreleased / "Knowledge Base/_Schema").mkdir(parents=True)
    (tmp_path / "items.md").write_text(manifest_text().replace(CID, DAILY))
    assert offline_create(unreleased, tmp_path / "items.md", DAILY_PATH, why="before the release") == 1
    assert capability.UNAVAILABLE in capsys.readouterr().err
    assert authority.read_marker(unreleased) is None and not connection.store_path(unreleased).exists()

    def create_daily(vault=root):
        return call(vault, "record_memory", action="create", manifest_path=DAILY_PATH, manifest_text=DAILY_TEXT,
                    why="summary")

    refused(create_daily)
    refused(lambda: imported(root, mode="preview", source_ref=SOURCE, format="ndjson", mapping=MAPPING))
    refused(lambda: titles(root, CID))
    assert call(root, "record_memory", action="append", collection=KEY, item={"title": "File row"},
                item_key=ROW, why="file write")["outcome"] == "committed"
    assert authority.read_marker(root) == marker and titles(root, KEY) == ["File row"]
    monkeypatch.setattr(capability, "RELEASED", ON)
    plain = tmp_path / "plain"
    (plain / "Knowledge Base/_Schema").mkdir(parents=True)
    refused(lambda: create_daily(plain), "COLLECTION_STORE_ENROLLMENT_REQUIRED")
    assert offline_create(root, tmp_path / "items.md", DAILY_PATH, why="items mode") == 1
    assert "COLLECTION_STORE_SUMMARY_REQUIRED" in capsys.readouterr().err
    (tmp_path / "daily.md").write_text(DAILY_TEXT)
    assert offline_create(root, tmp_path / "daily.md", DAILY_PATH, why="beside the service") == 1
    assert "COLLECTION_STORE_SERVICE_ACTIVE" in capsys.readouterr().err
    created = create_daily()
    assert authority.selected_entry(root, authority.parse_marker(root, authority.read_marker(root)), DAILY), created
    assert call(root, "record_memory", action="append", collection=DAILY, item={"title": "Daily row"},
                why="summary write")["outcome"] == "committed"
    assert titles(root, DAILY) == ["Daily row"]
    typed = call(root, "record_memory", action="query", collection=DAILY, query={"version": 1, "select": ["title"]})
    assert typed["rows"] == [{"title": "Daily row"}]
    preview = imported(root, DAILY, mode="preview", source_ref=SOURCE, format="ndjson", mapping=MAPPING)
    assert preview["rows"]["valid"] == 3
    monkeypatch.setattr(capability, "RELEASED", frozenset())
    refused(lambda: titles(root, DAILY))
    refused(lambda: call(root, "record_memory", action="inspect", collection=DAILY))
    inventory = call(root, "record_memory", action="inspect")
    assert DAILY not in {row["collection_id"] for row in inventory["collections"]}
    assert (DAILY_PATH, capability.UNAVAILABLE) in {
        (row["path"], row["error_code"]) for row in inventory["unreadable_manifests"]}
    assert titles(root, KEY) == ["File row"]


def test_global_inspection_rechecks_canonical_support_and_permissions_without_a_projection_refresh(service):
    """Shared inspection work must not retain a prior request's row or audience decisions."""
    from test_collection_claims import _write_observation
    from test_governance_egress import _external, write_rule, write_scope

    from exomem import due_state

    root = service.root
    observed = "77777777-7777-4777-8777-777777777777"
    observed_path = manifest_path().replace("Work", "Observed")
    observed_text = summary_text(manifest_text().replace(CID, observed).replace("title: Work", "title: Observed")
                                 .replace("    count: {type: integer}\n",
                                          "    count: {type: integer}\n    category: {type: string}\n"))
    call(root, "record_memory", action="create", manifest_path=observed_path,
         manifest_text=observed_text, why="observed routing")
    for key, title in ((ROW, "First"), (OTHER, "Second")):
        appended = call(root, "record_memory", action="append", collection=observed,
                        item_key=key, item={"title": title, "category": "account subscriptions"}, why="support")
    daily_text = DAILY_TEXT.replace("lifecycle: active\n",
                                    "lifecycle: active\nclaims:\n  terms: [device, inventory]\n")
    call(root, "record_memory", action="create", manifest_path=DAILY_PATH,
         manifest_text=daily_text, why="declared routing")
    call(root, "record_memory", action="append", collection=KEY,
         item={"title": "File row"}, item_key=ROW, why="coexisting file collection")
    observation, observation_ref = _write_observation(root)
    other_observation = observation.replace("account-observation", "device-observation")
    (root / other_observation).write_text((root / observation).read_text()
        .replace("44444444-4444-4444-8444-444444444444", "55555555-5555-4555-8555-555555555555")
        .replace("account", "device").replace("Account", "Device")
        .replace("subscription", "inventory").replace("Subscription", "Inventory"))
    server = runtime._SERVERS[root]
    with request_scope(OWNER):
        server.call(lambda: due_state.reconcile(root, now=dt.datetime(2026, 9, 3, 12, tzinfo=dt.UTC)))
    projection = due_state.state_path(root)
    original_projection = projection.read_bytes()

    inventory = call(root, "record_memory", action="inspect")
    rows = {row["collection_id"]: row for row in inventory["collections"]}
    assert set(rows) == {KEY, CID, DAILY, observed}
    assert {key: (row["committed"], row["unreflected"]) for key, row in rows.items()} == {
        KEY: (1, 0), CID: (1, 0), DAILY: (0, 1), observed: (2, 1),
    }
    coverage = call(root, "record_memory", action="inspect", collection=observed)["coverage"]
    assert coverage["unreflected_refs"] == [observation_ref]

    write_scope(root, paths="Unrelated/**")
    scope = root / "Knowledge Base/_Governance/scopes/patterns.yaml"
    scope.write_text(scope.read_text() + f"refs: [exomem://record/{observed}/{OTHER}]\n")
    write_rule(root, ceiling=0)
    command = next(command for command in commands.product_commands_for("mcp") if command.name == "record_memory")
    with request_scope(_external()):
        hidden = writer_lease.invoke_command(command, root, action="inspect")
    hidden_row = next(row for row in hidden["collections"] if row["collection_id"] == observed)
    assert (hidden_row["committed"], hidden_row["unreflected"]) == (1, 0)
    write_rule(root, ceiling=6)
    with request_scope(_external()):
        released = writer_lease.invoke_command(command, root, action="inspect")
    released_row = next(row for row in released["collections"] if row["collection_id"] == observed)
    assert (released_row["committed"], released_row["unreflected"]) == (2, 1)
    assert projection.read_bytes() == original_projection

    call(root, "record_memory", action="update", collection=observed, item_key=OTHER,
         changes={"category": "different material"}, why="remove recurring support",
         expected_container_hash=appended["after_container_hash"],
         expected_item_version=appended["after_item_hash"])
    current = call(root, "record_memory", action="inspect")
    assert next(row for row in current["collections"] if row["collection_id"] == observed)["unreflected"] == 0
    assert projection.read_bytes() == original_projection

    (root / other_observation).unlink()
    with request_scope(OWNER):
        server.call(lambda: due_state.reconcile(root))
    assert projection.read_bytes() != original_projection
    replaced = call(root, "record_memory", action="inspect")
    assert next(row for row in replaced["collections"] if row["collection_id"] == DAILY)["unreflected"] == 0


def test_expired_due_refresh_keeps_the_request_principal_and_canonical_ownership(service, monkeypatch):
    """File-backed routing under the service coordinator keeps the original audience and canonical read scope."""
    from test_collection_claims import _write_observation
    from test_governance_egress import _external

    from exomem import due_state
    from exomem.collection_store import ownership
    from exomem.governance.principal import current_principal

    root = service.root
    daily_text = DAILY_TEXT.replace("view_mode: summary\n", "").replace("lifecycle: active\n",
        "lifecycle: active\nclaims:\n  terms: [account, subscriptions]\n")
    call(root, "record_memory", action="create", manifest_path=DAILY_PATH,
         manifest_text=daily_text, why="declared routing")
    observation, _ref = _write_observation(root)
    now = dt.datetime(2026, 9, 3, 12, tzinfo=dt.UTC)
    with request_scope(OWNER):
        runtime._SERVERS[root].call(lambda: due_state.reconcile(root, now=now))
    due_state.reset_serve_cache()
    guest = _external()
    with request_scope(guest):
        before = due_state.served_entries(root, now=now, principal=guest)
    assert any(row["path"] == observation for row in before)
    real = due_state._served_entries_uncached
    observed = []
    workers = []

    def rebuilding(*args, **kwargs):
        rows = real(*args, **kwargs)
        if threading.current_thread().name == "exomem-due-state-serve-refresh":
            observed.append((current_principal(), ownership._READ.get() is not None, rows[0]))
            workers.append(threading.current_thread())
        return rows

    monkeypatch.setattr(due_state, "_served_entries_uncached", rebuilding)
    monkeypatch.setattr(due_state, "_SERVE_CACHE_TTL_SECONDS", 0)
    monkeypatch.delenv("EXOMEM_SYNC_DUE_STATE_WARM", raising=False)
    try:
        with request_scope(guest):
            assert due_state.served_entries(root, now=now, principal=guest) == before
        _until(lambda: bool(observed))
        for worker in workers:
            worker.join(5)
            assert not worker.is_alive()
        assert observed == [(guest, True, before)]
    finally:
        for worker in workers:
            worker.join(5)
        due_state.reset_serve_cache()


def test_the_service_builds_a_revised_rollup_and_publishes_the_summary_page(service, monkeypatch):
    """Defect: nothing in the service drives derived backfill, so a rollup that a governed revise
    adds to a populated collection stays building and every daily sum reads base rows, or the
    built rollup answers differently from them; or the summary page and the manifest view never
    publish after create, write and revise."""
    root = service.root
    monkeypatch.setattr(capability, "RELEASED", ON)
    activity = "77777777-7777-4777-8777-777777777777"
    path = manifest_path().replace("Work", "Activity")
    text = summary_text(manifest_text().replace(CID, activity).replace("title: Work", "title: Activity")
                        .replace("    count: {type: integer}\n", "    count: {type: integer}\n    day: {type: date}\n"))
    call(root, "record_memory", action="create", manifest_path=path, manifest_text=text, why="activity")
    for title, day, count in (("a", "2026-10-01", 3), ("b", "2026-10-01", 4), ("c", "2026-10-02", 5)):
        call(root, "record_memory", action="append", collection=activity, item={"title": title, "day": day,
                                                                                "count": count}, why="row")
    daily = {"version": 1, "group_by": [{"field": "day", "bucket": "day"}],
             "aggregates": {"total": {"op": "sum", "field": "count"}}}
    base = call(root, "record_memory", action="query", collection=activity, query=daily)
    assert base["plan"]["strategy"] == "base"
    guards = call(root, "record_memory", action="inspect", collection=activity)["lifecycle_guards"]
    rollup = "rollups:\n  daily:\n    bucket: day\n    timestamp: day\n    values:\n      count: [sum]\n"
    call(root, "record_memory", action="revise", collection=activity, manifest_text=text.removesuffix("---\n")
         + rollup + "---\n", why="daily rollup", **guards)
    _until(lambda: call(root, "record_memory", action="query", collection=activity,
                        query={**daily, "mode": "explain"})["plan"]["strategy"] == "rollup")
    rolled = call(root, "record_memory", action="query", collection=activity, query=daily)
    assert rolled["plan"]["strategy"] == "rollup" and rolled["groups"] == base["groups"]
    page = root / path.replace("_collection.md", "Items/_summary.md")
    _until(lambda: page.exists() and "3 committed rows" in page.read_text())
    _until(lambda: "rollups:" in (root / path).read_text())


def test_the_release_lever_stops_the_service_building_derived_state(service, monkeypatch):
    """Defect: with records-summary-v1 switched off, the service keeps writing a summary collection's
    derived state, so the slice's rollback lever cannot stop the store's background writes."""
    root = service.root
    monkeypatch.setattr(capability, "RELEASED", ON)
    monkeypatch.setattr(runtime, "BACKFILL_BATCH_ROWS", 1)
    activity = "99999999-9999-4999-8999-999999999999"
    text = summary_text(manifest_text().replace(CID, activity).replace("title: Work", "title: Lever")
                        .replace("    count: {type: integer}\n", "    count: {type: integer}\n    day: {type: date}\n"))
    call(root, "record_memory", action="create", manifest_path=manifest_path().replace("Work", "Lever"),
         manifest_text=text, why="lever")
    for n in range(30):
        call(root, "record_memory", action="append", collection=activity,
             item={"title": f"r{n}", "day": "2026-10-01", "count": n}, why="row")
    guards = call(root, "record_memory", action="inspect", collection=activity)["lifecycle_guards"]
    rollup = "rollups:\n  daily:\n    bucket: day\n    timestamp: day\n    values:\n      count: [sum]\n"
    call(root, "record_memory", action="revise", collection=activity, manifest_text=text.removesuffix("---\n")
         + rollup + "---\n", why="daily rollup", **guards)
    # The revise wakes the backfill tick, which may have begun a batch before the lever clears. The store
    # thread runs its ticks one at a time, so a tick that begins after the clear shows that batch has ended.
    cleared, settled = [], threading.Event()
    backfill_tick = runtime.StoreServer._backfill_tick

    def observed(self):
        after_clear = bool(cleared)
        try:
            return backfill_tick(self)
        finally:
            if after_clear:
                settled.set()

    monkeypatch.setattr(runtime.StoreServer, "_backfill_tick", observed)
    monkeypatch.setattr(capability, "RELEASED", frozenset())
    cleared.append(True)
    assert settled.wait(10), "the store thread never ran a tick after the lever cleared"

    def progress():
        with closing(connection.open_reader(connection.store_path(root))) as reader:
            return reader.execute("SELECT state,last_row_id FROM rollup_definitions WHERE collection_id=?",
                                  (activity,)).fetchone()

    stopped = progress()
    time.sleep(1.0)
    assert stopped[0] == "building" and progress() == stopped


def test_vault_rollback_survives_restart_and_keeps_file_collections_usable(service, monkeypatch, capsys):
    """Rollback must stop a pending import after restart without deleting rows or disabling A/B."""
    root = service.root
    monkeypatch.setattr(importer, "MAX_BATCH_ROWS", 1)
    write_source(root, ndjson({"title": f"Imported {i}", "count": i} for i in range(60)), SOURCE)
    imported(root, mode="start", source_ref=SOURCE, format="ndjson", mapping=MAPPING)
    monkeypatch.setattr(capability, "RELEASED", frozenset())
    service.stop()
    marker = authority.read_marker(root)
    monkeypatch.setenv(OPERATOR, "operator")
    assert cli._collections_main(["rollback", "--vault", str(root)]) == 0, capsys.readouterr().err
    monkeypatch.delenv(OPERATOR)
    monkeypatch.setattr(capability, "RELEASED", ON)

    def stored():
        with closing(connection.open_reader(connection.store_path(root))) as reader:
            return {table: reader.execute(f"SELECT * FROM {table}").fetchall()
                    # Closed schema tables: canonical rows, audit and each kind of background work.
                    for table in ("items", "txns", "import_jobs", "projection_state", "rollup_definitions")}

    before = stored()
    restarted = Service(root, writer_lease.start_server_lifecycle())
    try:
        _until(lambda: restarted.manager.status().get("collection_store", {}).get("status") == "admitted")
        refused(lambda: titles(root, CID), capability.DISABLED)
        time.sleep(1.2)
        assert stored() == before and authority.read_marker(root) == marker
        assert call(root, "record_memory", action="append", collection=KEY, item={"title": "After rollback"},
                    item_key=LATER, why="file write")["outcome"] == "committed"
        assert titles(root, KEY) == ["After rollback"]
    finally:
        restarted.stop()


def test_rollback_preserves_a_pending_later_create_without_recovering_it(service, monkeypatch):
    """A stopped producer must not require create recovery before the owner can disable it."""
    root = service.root
    with monkeypatch.context() as deadline:
        deadline.setattr(admission, "time", SimpleNamespace(
            monotonic=lambda: time.monotonic() - 31, time=time.time, sleep=time.sleep))
        created = call(root, "record_memory", action="create", manifest_path=DAILY_PATH,
                       manifest_text=DAILY_TEXT, why="summary", idempotency_key="pending-create")
        assert created["status"] == "committed"
        refused(service.stop, "COLLECTION_STORE_FLUSH_PENDING")
    # The close test proves renewal stops; surrender the stopped holder instead of waiting its TTL here.
    service.manager.client.release_holder(service.manager.config.replica_id, service.manager._fencing_token)
    marker = authority.read_marker(root)
    before = store_meta(root)[authority.PENDING_CREATE]
    monkeypatch.setenv(OPERATOR, "operator")
    with request_scope(OWNER):
        result = admission.rollback_route(root)
    assert result["status"] == "disabled" and result["publication"] == "published"
    assert authority.read_marker(root) == marker
    assert store_meta(root)[authority.PENDING_CREATE] == before
    assert capability.records_summary_disabled(root)
    monkeypatch.delenv(OPERATOR)
    restarted = Service(root, writer_lease.start_server_lifecycle())
    try:
        _until(lambda: restarted.manager._collection_store is not None)
        assert call(root, "record_memory", action="append", collection=KEY, item={"title": "Still writable"},
                    item_key=LATER, why="file write")["outcome"] == "committed"
        assert titles(root, KEY) == ["Still writable"]
        time.sleep(1.2)
        assert authority.read_marker(root) == marker
        assert store_meta(root)[authority.PENDING_CREATE] == before
    finally:
        try:
            restarted.stop()
        except OpError as error:
            assert error.code == "COLLECTION_STORE_FLUSH_PENDING"


def test_a_served_create_that_misses_its_publication_deadline_recovers_without_an_owner_step(
        service, monkeypatch):
    """Defect: a served create whose replica publication misses its deadline answers with an
    uncertain acknowledgement, and the store refuses that create's retry, every later write and
    the service stop until an owner resumes the create, which nothing lets the owner do."""
    root = service.root
    monkeypatch.setattr(capability, "RELEASED", ON)
    late = [True]  # every publication deadline has already passed when it starts, until reset
    monkeypatch.setattr(admission, "time", SimpleNamespace(
        monotonic=lambda: time.monotonic() - (31 if late[0] else 0), time=time.time, sleep=time.sleep))

    def create():
        return call(root, "record_memory", action="create", manifest_path=DAILY_PATH, manifest_text=DAILY_TEXT,
                    why="summary", idempotency_key="daily-create")

    def append():
        return call(root, "record_memory", action="append", collection=DAILY, item={"title": "Daily row"},
                    why="summary write")

    created = create()
    assert created["status"] == "committed" and created["warnings"][0].startswith("collection_publication_pending")
    refused(append, "COLLECTION_STORE_BUSY")  # retryable while the service keeps resuming the create
    # A restart while the create is still pending: the stop cannot hand off, so a new
    # process's service starts beside it, and its requests for the create stay busy.
    refused(service.stop, "COLLECTION_STORE_FLUSH_PENDING")
    runtime._SERVERS.pop(root, None)
    runtime._SERVED.pop(root, None)
    service = Service(root, writer_lease.start_server_lifecycle())
    codes = []

    def busy():
        try:
            call(root, "record_memory", action="inspect", collection=DAILY)
        except OpError as error:
            codes.append(error.code)
        return codes[-1:] == ["COLLECTION_STORE_BUSY"]

    _until(busy)
    assert "COLLECTION_NOT_FOUND" not in codes, codes
    marker = authority.marker_path(root)
    agreed = marker.read_bytes()
    marker.write_bytes(agreed + b"\n")  # a marker changed under the create never resolves by retrying
    refused(append, "COLLECTION_STORE_LEASE_REQUIRED")
    marker.write_bytes(agreed)
    late[0] = False

    def written():
        try:
            return append()["outcome"] == "committed"
        except OpError as error:
            assert error.code == "COLLECTION_STORE_BUSY"
            return False

    _until(written)
    assert create()["status"] == "committed" and titles(root, DAILY) == ["Daily row"]
    service.stop()  # the handoff publishes; a pending create would refuse it as FLUSH_PENDING


def test_unresolved_ownership_withholds_ambiguous_records_but_keeps_disjoint_notes_usable(service, tmp_path):
    """An unreadable intent cannot supply a complete negative for a collection-layer path."""
    root = service.root
    service.stop()
    with closing(sqlite3.connect(connection.store_path(root))) as conn:
        conn.execute("INSERT INTO store_meta(key,value) VALUES (?, '{\"version\": 1}')", (authority.PENDING_CREATE,))
        conn.commit()
    with coordinator(tmp_path / "replaced.sqlite"):  # a replaced coordinator keeps the store unopened
        runtime._SERVERS.pop(root, None)
        runtime._SERVED.pop(root, None)
        replaced = Service(root, writer_lease.start_server_lifecycle())
        try:
            _until(lambda: getattr(runtime._SERVERS.get(root), "refusal", None) is not None)

            before = ab_bytes(root)
            with pytest.raises(OpError, match="COLLECTION_STORE_UNAVAILABLE"):
                call(root, "record_memory", action="append", collection=KEY, item={"title": "File row"},
                     item_key=ROW, why="file write")
            assert ab_bytes(root) == before
            note = "Knowledge Base/Notes/Independent.md"
            (root / note).parent.mkdir(parents=True, exist_ok=True)
            (root / note).write_text("Ordinary note.\n")
            assert "Ordinary note" in call(root, "read_memory", path=note)["body"]
        finally:
            replaced.stop()


def test_lost_store_custody_keeps_file_writes_and_renewal_available(service, monkeypatch):
    """A store custody refusal must not disable knowledge and file collections behind its enrolled fence."""
    root = service.root
    service.stop()
    marker = authority.marker_path(root)
    before_marker = marker.read_bytes()
    protected = {path: path.read_bytes() for path in (root / manifest_path()).parent.rglob("*") if path.is_file()}
    before_head = service.manager.client.status().collection_store_head
    (root / "Knowledge Base/.stfolder").mkdir()
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_TTL", "3")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_IDLE_SECONDS", "0")
    runtime._SERVERS.pop(root, None)
    runtime._SERVED.pop(root, None)
    restarted = Service(root, writer_lease.start_server_lifecycle())
    try:
        _until(lambda: runtime._SERVERS[root].refusal[0] == "COLLECTION_STORE_CUSTODY_UNVERIFIED")
        assert call(root, "record_memory", action="append", collection=KEY,
                    item={"title": "File row after custody loss"}, item_key=LATER,
                    why="file authority remains available")["outcome"] == "committed"
        assert titles(root, KEY) == ["File row after custody loss"]

        note = {
            "content": "# Restart observation\n\n- [review state] File knowledge remains available.\n",
            "title": "Restart observation", "slug": "restart-observation", "suggestions": False,
        }
        draft = call(root, "remember", validate_only=True, **note)
        saved = call(root, "remember", draft_id=draft["draft_id"], draft_hash=draft["draft_hash"],
                     draft_token=draft["draft_token"], **note)
        assert "File knowledge remains available." in call(root, "read_memory", path=saved["path"], include_raw=True)["content"]

        acquired = restarted.manager.client.status()
        _until(lambda: restarted.manager.status()["last_renew_age_seconds"] is not None)
        renewed = restarted.manager.client.status()
        assert renewed.holder == acquired.holder == restarted.manager.config.replica_id
        assert renewed.fencing_token == acquired.fencing_token
        assert renewed.collection_store_head == before_head
        assert replica.file_writer_marker(root, renewed) is None  # GET is not an acquisition receipt.
        refused(lambda: call(root, "record_memory", action="append", collection=CID,
                             item={"title": "Must remain uncommitted"}, why="store custody is unavailable"),
                "COLLECTION_STORE_CUSTODY_UNVERIFIED")
        summary = manifest_path().replace("_collection.md", "Items/_summary.md")
        with pytest.raises((OpError, ValueError), match="COLLECTION_VIEW_PATH"):
            call(root, "manage_memory_file", operation="create", path=summary, overwrite=True,
                 content="# Generic overwrite\n\nDirect changes stay refused.\n")
        with request_scope(OWNER), restarted.manager.writer_authority_guard(vault_root=root):
            refused(lambda: vault.batch_atomic_write([
                vault.PlannedWrite(root / "Knowledge Base/unowned.md", "Unowned.\n",
                                   ensure_directories=((root / summary).parent / "nested",)),
            ], vault_root=root, post_commit_fanout=False), "COLLECTION_VIEW_PATH")
            refused(lambda: reserved_paths.move_generic_path(
                root, "Knowledge Base/Records", "Knowledge Base/Archived", source_kind="directory",
            ), "COLLECTION_VIEW_PATH")
        assert not ((root / summary).parent / "nested").exists()
        assert not (root / "Knowledge Base/Archived").exists()
        assert {path: path.read_bytes() for path in protected} == protected
        assert marker.read_bytes() == before_marker
        with request_scope(OWNER), restarted.manager.writer_authority_guard(vault_root=root):
            refuse_paths = authority.refuse_view_paths

            def removed_before_validation(*args, **kwargs):
                # Withdraw after the file proof without changing the renewal thread's marker reader.
                marker.unlink()
                return refuse_paths(*args, **kwargs)

            with monkeypatch.context() as gap:
                gap.setattr(authority, "refuse_view_paths", removed_before_validation)
                refused(lambda: vault.batch_atomic_write([
                    vault.PlannedWrite(root / "Knowledge Base/unowned.md", "Unpublished.\n"),
                ], vault_root=root, post_commit_fanout=False), "COLLECTION_STORE_MARKER_CONFLICT")
        marker.write_bytes(before_marker)
        assert not (root / "Knowledge Base/unowned.md").exists()
        (root / "Knowledge Base/_access.yaml").write_text("excluded:\n  - Records/Work/Items\n")
        with request_scope(OWNER), restarted.manager.writer_authority_guard(vault_root=root):
            with pytest.raises(ValueError, match="WRITE_REFUSED.*excluded"):
                vault.batch_atomic_write([vault.PlannedWrite(root / summary, "Unpublished.\n")],
                                         vault_root=root, post_commit_fanout=False)
    finally:
        marker.write_bytes(before_marker)
        restarted.stop()
    assert restarted.manager.client.status().collection_store_head == before_head


@pytest.mark.parametrize("withdrawal", ["removed", "malformed", "legacy", "changed"])
def test_a_file_publication_rechecks_its_acquired_marker_after_staging(service, monkeypatch, withdrawal):
    """A fallback granted before staging cannot survive a withdrawn or changed routing basis."""
    root = service.root
    service.stop()
    marker = authority.marker_path(root)
    before = marker.read_bytes()
    (root / "Knowledge Base/.stfolder").mkdir()
    runtime._SERVERS.pop(root, None)
    runtime._SERVED.pop(root, None)
    restarted = Service(root, writer_lease.start_server_lifecycle())
    validate = vault._validate_batch_write_access
    staged = []

    def withdraw(*args, **kwargs):
        validate(*args, **kwargs)
        if not staged:
            staged.append(True)
            if withdrawal == "removed":
                marker.unlink()
            elif withdrawal == "malformed":
                marker.write_bytes(b"{")
            elif withdrawal == "legacy":
                marker.write_text(json.dumps({**json.loads(before), "version": 1}))
            else:
                marker.write_bytes(before + b"\n")

    target = root / "Knowledge Base/unpublished.md"
    try:
        _until(lambda: runtime._SERVERS[root].refusal[0] == "COLLECTION_STORE_CUSTODY_UNVERIFIED")
        with request_scope(OWNER), restarted.manager.writer_authority_guard(vault_root=root):
            monkeypatch.setattr(vault, "_validate_batch_write_access", withdraw)
            with pytest.raises(OpError, match="WRITER_LEASE_REQUIRED"):
                vault.batch_atomic_write([vault.PlannedWrite(target, "Unpublished.\n")],
                                         vault_root=root, post_commit_fanout=False)
        assert staged and not target.exists()
    finally:
        marker.write_bytes(before)
        restarted.stop()


def test_no_runtime_file_authority_requires_current_replica_bytes_and_intent_at_the_same_head(service, monkeypatch):
    """Unpublished export metadata and replaced replicas do not change the business head but withdraw file authority."""
    root = service.root
    service.stop()
    marker = authority.read_marker(root)
    original = store_meta(root)
    installed = replica.replica_path(root)
    before_replica = installed.read_bytes()
    before_head = service.manager.client.status().collection_store_head
    (root / "Knowledge Base/.stfolder").mkdir()
    runtime._SERVERS.pop(root, None)
    runtime._SERVED.pop(root, None)
    restarted = Service(root, writer_lease.start_server_lifecycle())

    def append():
        return call(root, "record_memory", action="append", collection=KEY,
                    item={"title": "Unrelated file row"}, item_key=LATER, why="file authority")

    try:
        _until(lambda: runtime._SERVERS[root].refusal[0] == "COLLECTION_STORE_CUSTODY_UNVERIFIED")
        assert restarted.manager.ensure_writer().granted
        intent = {"version": 2, "kind": authority.EXPORT, "request_id": "export:interrupted",
                  "plan_id": "unpublished-export", "expected_marker": marker.decode(), "why": "return to files",
                  "exported_at": "20261018T000000Z",
                  "collections": [{name: entry[name] for name in ("collection_id", "manifest_path", "source_path")}
                                  for entry in json.loads(marker)["collections"]]}
        with closing(sqlite3.connect(connection.store_path(root))) as store:
            store.execute("INSERT INTO store_meta(key,value) VALUES (?,?)", (authority.PENDING_CREATE, json.dumps(intent)))
            store.commit()
        assert store_meta(root)[schema.META_COMMIT_SEQ] == original[schema.META_COMMIT_SEQ]
        assert replica._intent_digest(store_meta(root)) != original.get(schema.META_PUBLISHED_INTENT_SHA256)
        refused(append, "WRITER_LEASE_REQUIRED")
        with closing(sqlite3.connect(connection.store_path(root))) as store:
            store.execute("DELETE FROM store_meta WHERE key=?", (authority.PENDING_CREATE,))
            store.commit()
        installed.write_bytes(before_replica + b"unpublished")
        refused(append, "WRITER_LEASE_REQUIRED")
        installed.write_bytes(before_replica)
        assert append()["outcome"] == "committed"
        assert restarted.manager.client.status().collection_store_head == before_head
    finally:
        installed.write_bytes(before_replica)
        restarted.stop()


def test_no_runtime_file_authority_requires_every_canonical_collection_and_its_actual_source(service):
    """A syntactically valid marker cannot omit canonical ownership or move it away from the actual source."""
    root = service.root
    call(root, "record_memory", action="create", manifest_path=DAILY_PATH, manifest_text=DAILY_TEXT,
         why="a second canonical collection")
    service.stop()
    marker = authority.marker_path(root)
    before = marker.read_bytes()
    complete = json.loads(before)
    before_files = {path: path.read_bytes() for path in (root / A_PATH).parent.rglob("*") if path.is_file()}
    (root / "Knowledge Base/.stfolder").mkdir()
    runtime._SERVERS.pop(root, None)
    runtime._SERVED.pop(root, None)
    restarted = Service(root, writer_lease.start_server_lifecycle())
    try:
        _until(lambda: runtime._SERVERS[root].refusal[0] == "COLLECTION_STORE_CUSTODY_UNVERIFIED")
        omitted = {**complete, "collections": [entry for entry in complete["collections"] if entry["collection_id"] != CID]}
        changed = json.loads(before)
        changed["collections"][0]["source_path"] = "Knowledge Base/Records/OtherSource"
        for invalid in (omitted, changed):
            raw = json.dumps(invalid).encode()
            assert authority.parse_marker(root, raw)["version"] == 2
            marker.write_bytes(raw)
            refused(lambda: call(root, "record_memory", action="append", collection=KEY,
                                 item={"title": "Must stay uncommitted"}, item_key=LATER,
                                 why="ownership is unverified"), "WRITER_LEASE_REQUIRED")
        assert {path: path.read_bytes() for path in (root / A_PATH).parent.rglob("*") if path.is_file()} == before_files
    finally:
        marker.write_bytes(before)
        restarted.stop()


def test_the_file_ownership_reader_exposes_declarations_and_head_without_item_payloads(service):
    """The fallback metadata reader must not turn custody refusal into general canonical row access."""
    root = service.root
    marker = authority.parse_marker(root, authority.read_marker(root))
    with closing(connection.open_ownership_reader(connection.store_path(root))) as reader:
        reader.execute("BEGIN")
        assert {row[0] for row in reader.execute("SELECT collection_id FROM collections")} == {CID}
        for entry in marker["collections"]:
            authority.require_selected(reader, marker, entry, root=root)
        with pytest.raises(sqlite3.DatabaseError):
            reader.execute("SELECT body FROM items").fetchall()
        with pytest.raises(sqlite3.DatabaseError):
            reader.execute("SELECT created_txn FROM collections").fetchall()
        with pytest.raises(sqlite3.DatabaseError):
            reader.execute("UPDATE store_meta SET value='changed' WHERE key='store_id'")
    with closing(connection.open_head_reader(connection.store_path(root))) as reader:
        with pytest.raises(sqlite3.DatabaseError):
            reader.execute("SELECT collection_id FROM collections").fetchall()


def test_a_direct_file_move_rechecks_fallback_authority_after_its_plan(service, monkeypatch):
    """A binary move has no later Markdown batch to notice a marker withdrawn after its plan."""
    from exomem import move_file

    root = service.root
    service.stop()
    marker = authority.marker_path(root)
    before = marker.read_bytes()
    source, destination = "Knowledge Base/ordinary.bin", "Knowledge Base/moved.bin"
    (root / source).write_bytes(b"ordinary bytes")
    (root / "Knowledge Base/.stfolder").mkdir()
    runtime._SERVERS.pop(root, None)
    runtime._SERVED.pop(root, None)
    restarted = Service(root, writer_lease.start_server_lifecycle())
    rename = move_file._held_rename

    def withdraw(*args, **kwargs):
        marker.unlink()
        return rename(*args, **kwargs)

    try:
        _until(lambda: runtime._SERVERS[root].refusal[0] == "COLLECTION_STORE_CUSTODY_UNVERIFIED")
        monkeypatch.setattr(move_file, "_held_rename", withdraw)
        with pytest.raises((OpError, ValueError), match="WRITER_LEASE_REQUIRED"):
            call(root, "manage_memory_file", operation="move", old_path=source,
                 new_path=destination, update_wikilinks=False)
        assert (root / source).read_bytes() == b"ordinary bytes" and not (root / destination).exists()
    finally:
        marker.write_bytes(before)
        restarted.stop()


def test_a_failed_delete_restores_its_bytes_after_the_fallback_marker_is_withdrawn(service, monkeypatch):
    """A checkpoint failure after the trash rename must not strand bytes when the forward grant disappears."""
    from exomem import graph_sync

    root = service.root
    service.stop()
    marker = authority.marker_path(root)
    before = marker.read_bytes()
    source = "Knowledge Base/ordinary.bin"
    (root / source).write_bytes(b"original bytes")
    (root / "Knowledge Base/.stfolder").mkdir()
    runtime._SERVERS.pop(root, None)
    runtime._SERVED.pop(root, None)
    restarted = Service(root, writer_lease.start_server_lifecycle())

    def fail_after_rename(transition):
        assert transition.renamed and not (root / source).exists()
        marker.unlink()
        raise PermissionError("checkpoint unavailable")

    try:
        _until(lambda: runtime._SERVERS[root].refusal[0] == "COLLECTION_STORE_CUSTODY_UNVERIFIED")
        monkeypatch.setattr(graph_sync.GraphLifecycleTransition, "publish_checkpoint", fail_after_rename)
        with pytest.raises(ValueError, match="GRAPH_SYNC_CHECKPOINT_FAILED"):
            call(root, "manage_memory_file", operation="delete", path=source, confirm=True)
        assert (root / source).read_bytes() == b"original bytes"
        assert not list((root / "Knowledge Base/_trash").rglob("ordinary.bin"))
    finally:
        marker.write_bytes(before)
        restarted.stop()


def test_a_failed_move_restores_its_bytes_after_the_fallback_marker_is_withdrawn(service, monkeypatch):
    """A failed link publication must still undo its completed rename after fallback authority disappears."""
    from exomem import move_file

    root = service.root
    service.stop()
    marker = authority.marker_path(root)
    before = marker.read_bytes()
    source, destination = "Knowledge Base/ordinary.md", "Knowledge Base/moved.md"
    original = "# Original\n\nOrdinary content.\n"
    (root / source).write_text(original)
    (root / "Knowledge Base/referrer.md").write_text("# Referrer\n\n[[ordinary]]\n")
    (root / "Knowledge Base/.stfolder").mkdir()
    runtime._SERVERS.pop(root, None)
    runtime._SERVED.pop(root, None)
    restarted = Service(root, writer_lease.start_server_lifecycle())

    def fail_after_rename(*args, **kwargs):
        assert not (root / source).exists() and (root / destination).read_text() == original
        marker.unlink()
        raise PermissionError("link publication unavailable")

    try:
        _until(lambda: runtime._SERVERS[root].refusal[0] == "COLLECTION_STORE_CUSTODY_UNVERIFIED")
        monkeypatch.setattr(move_file, "batch_atomic_write", fail_after_rename)
        with pytest.raises(PermissionError, match="link publication unavailable"):
            call(root, "manage_memory_file", operation="move", old_path=source, new_path=destination)
        assert (root / source).read_text() == original and not (root / destination).exists()
        assert (root / "Knowledge Base/referrer.md").read_text() == "# Referrer\n\n[[ordinary]]\n"
    finally:
        marker.write_bytes(before)
        restarted.stop()


def test_a_revise_cannot_turn_an_empty_summary_collection_into_an_items_mode_one(service):
    """Defect: once its summary page publishes, an empty summary collection revised without
    view_mode becomes an items-mode store collection outside the release gate."""
    root = service.root
    empty = "88888888-8888-4888-8888-888888888888"
    path = manifest_path().replace("Work", "Empty")
    text = summary_text(manifest_text().replace(CID, empty).replace("title: Work", "title: Empty"))
    call(root, "record_memory", action="create", manifest_path=path, manifest_text=text, why="empty")

    def published():
        with closing(connection.open_reader(connection.store_path(root))) as reader:
            return reader.execute("SELECT state FROM projection_state WHERE collection_id=? AND kind='summary'",
                                  (empty,)).fetchone() == ("current",)

    _until(published)  # no pending publication masks a mode change any more
    guards = call(root, "record_memory", action="inspect", collection=empty)["lifecycle_guards"]
    refused(lambda: call(root, "record_memory", action="revise", collection=empty, why="items mode",
                         manifest_text=text.replace("view_mode: summary\n", ""), **guards),
            "COLLECTION_STORE_SUMMARY_REQUIRED")


def test_a_collection_whose_publication_is_refused_does_not_stall_the_views_of_others(service, caplog):
    """Defect: a withheld row refuses its collection's whole publishing write, and that refusal stops a
    collection sorting after it from publishing its new views."""
    root = service.root

    def pending(cid):
        with closing(connection.open_reader(connection.store_path(root))) as reader:
            return reader.execute("SELECT COUNT(*) FROM projection_state WHERE collection_id=? AND state='pending'",
                                  (cid,)).fetchone()[0]

    call(root, "record_memory", action="create", manifest_path=DAILY_PATH, manifest_text=DAILY_TEXT,
         why="a collection that sorts before Work")
    call(root, "record_memory", action="append", collection=DAILY, item={"title": "Daily row"}, why="first row")
    _until(lambda: pending(DAILY) == 0 and pending(CID) == 0)
    # A summary row's path is its manifest, so withholding it refuses every write to Daily.
    (root / "Knowledge Base/_access.yaml").write_text("excluded: [Records/Daily/_collection.md]\n")
    call(root, "record_memory", action="append", collection=CID, item={"title": "Later row"}, item_key=LATER,
         why="a write beside the refused collection")
    _until(lambda: pending(CID) == 0)
    assert titles(root, CID) == ["Canonical", "Later row"]
    assert pending(DAILY) > 0  # visibly pending, not published
    assert "could not publish" not in caplog.text


def test_a_coordinator_that_never_enrolled_the_store_names_adopt_local_which_serves_it(
        service, tmp_path, capsys):
    """Defect: a vault whose coordinator never enrolled its store (a copy, or a replaced coordinator)
    tells the owner to run `collections create`, which refuses CREATE_CONFLICT; the refusal repeats
    its code; or adopt-local, run in the same process after the service stops, refuses as opening."""
    root = service.root
    service.stop()
    with coordinator(tmp_path / "replaced-coordinator.sqlite"):
        replaced = Service(root, writer_lease.start_server_lifecycle())
        try:
            with pytest.raises(OpError) as refusal:
                titles(root, CID)
        finally:
            replaced.stop()
        assert refusal.value.code == "COLLECTION_STORE_ADOPTION_REQUIRED"
        assert "exomem collections adopt-local" in refusal.value.message
        assert refusal.value.code not in refusal.value.message
        with pytest.MonkeyPatch.context() as shell:  # the owner's offline step, in this same process
            shell.setenv(OPERATOR, "operator")
            adopt = ["adopt-local", "--vault", str(root), "--why", "the coordinator was replaced"]
            assert cli._collections_main([*adopt, "--dry-run"]) == 0, capsys.readouterr().err
            preview = json.loads(capsys.readouterr().out)
            assert cli._collections_main([*adopt, "--preview-id", preview["preview_id"]]) == 0, capsys.readouterr().err
        adopted = Service(root, writer_lease.start_server_lifecycle())
        try:
            assert titles(root, CID) == ["Canonical"]
        finally:
            adopted.stop()


def test_a_host_whose_marker_predates_a_retire_refuses_as_retired_not_adopt_local(service, tmp_path):
    """Defect: a host still holding the store marker after the coordinator retired the fence says
    the coordinator never enrolled the store and names adopt-local, which refuses that marker."""
    root = service.root
    service.stop()
    marker = authority.parse_marker(root, authority.read_marker(root))
    SQLiteLeaseStore(tmp_path / "coordinator.sqlite").retire_collection_store_fence(
        "gate", expected_generation=marker["collection_store_fence"]["generation"],
        capability="collections-store-v1", store_id=marker["store_id"])
    stale = Service(root, writer_lease.start_server_lifecycle())
    try:
        with pytest.raises(OpError) as refusal:
            titles(root, CID)
    finally:
        stale.stop()
    assert refusal.value.code == "COLLECTION_STORE_RETIRED"
    assert "adopt-local" not in refusal.value.message


def _finish_after_restart(found, continuation):
    """The restarted service, in a fresh interpreter: nothing here calls run_jobs."""
    capability.RELEASED = ON
    manager = writer_lease.start_server_lifecycle()
    try:
        deadline = time.monotonic() + 60
        while (status := imported(found.root, mode="status", continuation=continuation))["state"] == "running":
            assert time.monotonic() < deadline, status
            time.sleep(0.1)
    finally:
        manager.close()
    return status["state"], status["rows"]["imported"]


def test_an_import_started_before_a_restart_completes_on_the_restarted_service(service, tmp_path, monkeypatch):
    """Defect: import jobs advance only when something outside the service calls run_jobs, so a
    restart strands a running import."""
    root = service.root
    monkeypatch.setattr(importer, "MAX_BATCH_ROWS", 5)
    monkeypatch.setattr(capability, "RELEASED", ON)
    write_source(root, ndjson({"title": f"Imported {i}", "count": i} for i in range(60)), SOURCE)
    job = imported(root, mode="start", source_ref=SOURCE, format="ndjson", mapping=MAPPING)
    monkeypatch.setattr(capability, "RELEASED", frozenset())  # this service stops advancing it
    service.stop()
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        state, progress = reader.execute("SELECT state,progress_json FROM import_jobs").fetchone()
    assert state == "running" and json.loads(progress)["imported"] < 60
    restarted = functools.partial(_finish_after_restart, continuation=job["continuation"])
    assert run_host(tmp_path, tmp_path / "state", "restart", restarted, root=root,
                    database=tmp_path / "coordinator.sqlite") == ("complete", 60)


def test_import_status_polling_does_not_restart_blocked_summary_publication(service, monkeypatch):
    """Progress checks must not make the store actor repeatedly wait on a busy vault."""
    import threading

    root = service.root
    write_source(root, ndjson({"title": f"Imported {i}", "count": i} for i in range(1001)), SOURCE)
    job = imported(root, mode="start", source_ref=SOURCE, format="ndjson", mapping=MAPPING)

    def complete():
        with closing(connection.open_reader(connection.store_path(root))) as reader:
            return reader.execute("SELECT state FROM import_jobs").fetchone()[0] == "complete"

    _until(complete)
    initial = imported(root, mode="status", continuation=job["continuation"])
    assert initial["rows"]["imported"] == 1001
    appended, resume = threading.Event(), threading.Event()
    serve = runtime.StoreServer._serve

    def pending_before_maintenance(server, request):
        serve(server, request)
        if isinstance(request.result, dict) and request.result.get("item_key") == LATER:
            appended.set()
            assert resume.wait(5), "the test did not release the store actor"

    monkeypatch.setattr(runtime.StoreServer, "_serve", pending_before_maintenance)
    boundary = service.manager._mutation_coordinator_for(root)
    ordinary_timeout = service.manager._mutation_timeout_seconds
    guard = service.manager.consistency_guard
    rejected_operations = []

    @contextmanager
    def observed_guard(*args, **kwargs):
        entered = False
        try:
            with guard(*args, **kwargs) as held:
                entered = True
                yield held
        except OpError as error:
            if not entered and error.code == "MUTATION_BUSY":
                rejected_operations.append(kwargs.get("operation"))
            raise

    monkeypatch.setattr(service.manager, "consistency_guard", observed_guard)

    def refusals():
        # These two operation labels belong to the complete ownership proof protocol.
        return [operation for operation in rejected_operations
                if operation not in ("collection_ownership_read", "collection_ownership_release")]

    try:
        assert call(root, "record_memory", action="append", collection=CID, item_key=LATER,
                    item={"title": "Pending summary"}, why="pending summary")["outcome"] == "committed"
        assert appended.wait(2)
        # The independent replica publisher can still own the boundary during setup.
        with boundary.hold(timeout_seconds=ordinary_timeout, operation="test-summary-holder", holder_kind="foreground"):
            with monkeypatch.context() as held_timeout:
                # Keep only the deliberate refusal inside the normal maintenance backoff window.
                held_timeout.setattr(service.manager, "_mutation_timeout_seconds", 0.1)
                resume.set()
                _until(lambda: bool(refusals()))
                with pytest.raises(OpError) as busy:
                    imported(root, mode="status", continuation=job["continuation"])
                assert busy.value.code == "MUTATION_BUSY"
                assert len(refusals()) == 1
        for _ in range(3):
            status = imported(root, mode="status", continuation=job["continuation"])
            assert status["state"] == "complete" and status["rows"] == initial["rows"]
            time.sleep(0.15)
        assert len(refusals()) == 1
    finally:
        resume.set()
    page = root / manifest_path().replace("_collection.md", "Items/_summary.md")
    # An ordinary mutation still wakes publication after the held boundary clears.
    call(root, "record_memory", action="append", collection=CID,
         item={"title": "Publication resumed"}, why="resume publication")
    _until(lambda: page.exists() and "1004 committed rows" in page.read_text())


def test_served_field_release_and_summary_use_canonical_authority_without_preview_binding(service):
    """Public reads or governance that fall through to physical C bytes outside an explicit preview binding."""
    import yaml
    from test_collection_field_admission import GRANT, GUEST, release_document

    from exomem import vault

    root = service.root
    front, body, _ = vault.parse_frontmatter(summary_text(), strict=True)
    front["item_schema"]["fields"].update({
        "place": {"type": "object", "classification": "location", "properties": {"label": {"type": "string"}}},
        "label": {"type": "string", "depends_on": ["place"]},
    })
    text = "---\n" + yaml.safe_dump(front, sort_keys=False) + "---\n" + body
    guards = call(root, "record_memory", action="inspect", collection=CID)["lifecycle_guards"]
    call(root, "record_memory", action="revise", collection=CID, manifest_text=text,
         why="declare reviewed location", **guards)
    call(root, "record_memory", action="append", collection=CID, item_key=LATER,
         item={"title": "Located", "place": {"label": "invented-place"}, "label": "invented-place"},
         why="record location")
    path = manifest_path().replace("_collection.md", "Items/_summary.md")
    _until(lambda: (root / path).exists())

    def recipient(tool, **arguments):
        command = next(command for command in commands.product_commands_for("mcp") if command.name == tool)
        with request_scope(GUEST):
            return writer_lease.invoke_command(command, root, **arguments)

    page = recipient("read_memory", path=path.removeprefix("Knowledge Base/").removesuffix(".md"))
    assert page["frontmatter"]["metric"] == "released rows"
    assert page["frontmatter"]["value"] == 2
    assert "exomem_view" not in page["frontmatter"]
    with pytest.raises(ValueError, match="NOT_FOUND"):
        recipient("read_memory", path=manifest_path())

    def summary_published():
        with closing(connection.open_reader(connection.store_path(root))) as reader:
            return reader.execute("SELECT state FROM projection_state WHERE path=?", (path,)).fetchone() == ("current",)

    # The owner reads physical bytes, which must match the latest canonical render.
    _until(summary_published)
    assert "exomem_view" in call(root, "read_memory", path=path)["frontmatter"]
    assert recipient("read_memory", path=A_PATH)["path"] == A_PATH
    basis = call(root, "record_memory", action="inspect", collection=CID)["field_release_basis"]
    proposed = call(root, "govern_memory", operation="propose", intent="Release invented location",
                    documents={"grants/collection-location.yaml": release_document(basis)},
                    selector_paths=[basis["path"]], target_ceiling=6, duration="standing")
    assert proposed["ok"], proposed
    committed = call(root, "govern_memory", operation="commit", proposal_id=proposed["diagnostics"]["proposal_id"])
    assert committed["ok"], committed
    rows = recipient("record_memory", action="query", collection=CID,
                     query={"version": 1, "select": ["place", "label"]})["rows"]
    assert any(row.get("place") == {"label": "invented-place"} for row in rows)
    assert call(root, "govern_memory", operation="revoke", scope="standing", grant_id=GRANT)["ok"]
    refused(lambda: recipient("record_memory", action="query", collection=CID,
                              query={"version": 1, "select": ["place"]}), "QUERY_FIELD_UNAVAILABLE")
    service.stop()
    assert recipient("read_memory", path=A_PATH)["path"] == A_PATH
    refused(lambda: recipient("read_memory", path=path), "COLLECTION_STORE_UNAVAILABLE")


def test_unavailable_canonical_ownership_keeps_disjoint_notes_available(service, tmp_path):
    """Corrupt canonical state cannot turn ambiguous collection-layer paths into ordinary files."""
    from test_collection_field_admission import GUEST

    root = service.root
    external = "Knowledge Base/Records/DetachedRows"
    text = DAILY_TEXT.replace("source: Items", "source: " + external)
    call(root, "record_memory", action="create", manifest_path=DAILY_PATH, manifest_text=text, why="external source")
    marker = authority.parse_marker(root, authority.read_marker(root))
    assert marker["version"] == 2
    assert authority.owned_entry(root, marker, external + "/unknown.md")["collection_id"] == DAILY
    service.stop()
    protected = [manifest_path(), "Knowledge Base/Records/Work/Items/_summary.md",
                 external + "/unknown.md", "Knowledge Base/Records/Daily/Held/unknown.md",
                 "Knowledge Base/Records/Daily/_history/unknown.md", "Knowledge Base/Records/Daily/_history.md"]
    for path in protected:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text("# Edited view\n\nprivate-projection-content\n")
    ordinary = "Knowledge Base/Records/Daily/Notes.md"
    (root / ordinary).write_text("# Independent knowledge\n\nordinary-content\n")
    note = root / "Knowledge Base/Notes/Independent.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Independent knowledge\n\nordinary-content\n")
    connection.store_path(root).write_bytes(b"unreadable sqlite fixture")
    restarted = Service(root, writer_lease.start_server_lifecycle())
    try:
        _until(lambda: runtime._SERVERS.get(root) is not None and runtime._SERVERS[root].runtime is None)
        assert "ordinary-content" in call(root, "read_memory", path=note.relative_to(root).as_posix())["body"]
        command = next(command for command in commands.product_commands_for("mcp") if command.name == "read_memory")
        for who in (OWNER, GUEST):
            with request_scope(who):
                for path in [*protected, A_PATH, B_PATH, ordinary]:
                    try:
                        result = writer_lease.invoke_command(command, root, path=path)
                    except (OpError, CollectionStoreError, ValueError) as error:
                        assert "private-projection-content" not in str(error)
                    else:
                        pytest.fail(f"unavailable owned path returned content: {path}: {result}")
    finally:
        restarted.stop()


@pytest.mark.parametrize("phase", ["before", "after"])
def test_legacy_marker_upgrade_recovers_after_atomic_publication_interruption(service, monkeypatch, phase):
    """A restart that derives ownership from edited views or leaves an interrupted v1 upgrade ambiguous."""
    root = service.root
    service.stop()
    marker = authority.parse_marker(root, authority.read_marker(root))
    marker["version"] = 1
    for entry in marker["collections"]:
        entry.pop("source_path")
        entry.pop("layout")
    authority.marker_path(root).write_text(json.dumps(marker))
    (root / manifest_path()).write_text("# Edited view without declaration\n")
    publish = admission._replace_marker
    interrupted = []

    def interrupt(session, token, expected, raw):
        if not interrupted and phase == "before":
            interrupted.append(True)
            raise OSError("injected marker publication interruption")
        publish(session, token, expected, raw)
        if not interrupted:
            interrupted.append(True)
            raise OSError("injected marker publication interruption")

    monkeypatch.setattr(admission, "_replace_marker", interrupt)
    restarted = Service(root, writer_lease.start_server_lifecycle())
    try:
        _until(lambda: runtime._SERVERS.get(root) is not None and runtime._SERVERS[root].runtime is not None)
        assert titles(root, CID) == ["Canonical"]
        _until(lambda: restarted.manager.status().get("collection_store", {}).get("status") == "admitted")
        upgraded = authority.parse_marker(root, authority.read_marker(root))
        assert interrupted and upgraded["version"] == 2
        assert upgraded["authority_epoch"] == marker["authority_epoch"] + 1
        assert upgraded["collections"][0]["source_path"] == "Knowledge Base/Records/Work/Items"
        assert titles(root, CID) == ["Canonical"]
        assert state_migration.COLLECTION_MARKER_COMPATIBILITY_ID in state_migration.recorded_descriptor_ids(root)
    finally:
        restarted.stop()


def test_new_marker_entry_cannot_overlap_existing_owned_source(service):
    """A new collection that changes which canonical collection authorizes an already owned source subtree."""
    root = service.root
    marker = authority.read_marker(root)
    text = DAILY_TEXT.replace("source: Items", "source: Knowledge Base/Records/Work/Items/Nested")
    refused(lambda: call(root, "record_memory", action="create", manifest_path=DAILY_PATH,
                         manifest_text=text, why="overlapping source"), "COLLECTION_STORE_MARKER_CONFLICT")
    assert authority.read_marker(root) == marker and titles(root, CID) == ["Canonical"]


def test_new_c_cannot_take_a_file_collection_source_or_mutation_authority(service):
    """An absent C source below A's items that steals A's public writes and contaminates its history."""
    root = service.root
    assert call(root, "record_memory", action="append", collection=KEY, item={"title": "Before"},
                item_key=ROW, why="file baseline")["outcome"] == "committed"
    before = call(root, "record_memory", action="inspect", collection=KEY)
    marker = authority.read_marker(root)
    source = "Knowledge Base/Records/Legacy/Items/Nested"
    assert not (root / source).exists()
    text = DAILY_TEXT.replace("source: Items", "source: " + source)
    refused(lambda: call(root, "record_memory", action="create", manifest_path=DAILY_PATH,
                         manifest_text=text, why="overlapping file source"), "COLLECTION_STORE_MARKER_CONFLICT")
    assert authority.read_marker(root) == marker
    assert not (root / DAILY_PATH).exists() and not (root / source).exists()
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        assert reader.execute("SELECT collection_id FROM collections ORDER BY collection_id").fetchall() == [(CID,)]
        assert authority.pending_create(reader) is None
    after = call(root, "record_memory", action="inspect", collection=KEY)
    assert after["source_versions"] == before["source_versions"] and after["audit"] == before["audit"]
    assert call(root, "record_memory", action="append", collection=KEY, item={"title": "After"},
                item_key=LATER, why="file still writable")["outcome"] == "committed"
    assert titles(root, KEY) == ["After", "Before"] and titles(root, CID) == ["Canonical"]


def test_new_c_requires_readable_file_ownership_and_succeeds_after_declaration_repair(service):
    """Skipping an unreadable file declaration that could own the proposed source namespace."""
    root = service.root
    original = (root / A_PATH).read_bytes()
    marker = authority.read_marker(root)
    (root / A_PATH).write_text("not a collection declaration")
    refused(lambda: call(root, "record_memory", action="create", manifest_path=DAILY_PATH,
                         manifest_text=DAILY_TEXT, why="new separate collection"), "COLLECTION_STORE_MARKER_CONFLICT")
    assert authority.read_marker(root) == marker and not (root / DAILY_PATH).exists()
    (root / A_PATH).write_bytes(original)
    assert call(root, "record_memory", action="create", manifest_path=DAILY_PATH,
                manifest_text=DAILY_TEXT, why="file declaration repaired")["status"] == "committed"
    assert call(root, "record_memory", action="append", collection=KEY, item={"title": "Still writable"},
                item_key=ROW, why="file authority retained")["outcome"] == "committed"


def test_mixed_vault_owner_reads_original_bytes_and_recipient_still_cannot_read_protected_raw(service):
    """A vault-wide C gate that sends unrelated original bytes through the Markdown-only bridge gate."""
    import hashlib

    from test_collection_field_admission import GUEST

    root = service.root
    body = '{"measurement":7}\n'
    receipt = call(root, "preserve_evidence", scope="Test", category="export", filename="original.ndjson",
                   content=body, raw_protection=True, response_detail="full")["diagnostics"]
    original = receipt["path"]
    ordinary = "Knowledge Base/Records/Work/ordinary.ndjson"
    (root / ordinary).write_text(body)
    for path in (original, ordinary):
        page = call(root, "read_memory", path=path, include_raw=True)
        assert page["content"] == body
        assert page["content_hash"] == hashlib.sha256(body.encode()).hexdigest()
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "read_memory")
    denials = []
    with request_scope(GUEST):
        for path in (original, original + ".missing"):
            with pytest.raises(ValueError) as error:
                writer_lease.invoke_command(command, root, path=path, include_raw=True)
            denials.append(str(error.value).replace(path, "<requested>"))
        assert writer_lease.invoke_command(command, root, path=ordinary, include_raw=True)["content"] == body
    assert denials[0] == denials[1]


@pytest.mark.parametrize("arrival", ["after_discovery", "before_commit"])
def test_new_file_declaration_arrival_cannot_lose_its_authority_to_c(service, monkeypatch, arrival):
    """A file declaration arriving after discovery must survive the C transaction's ownership recheck."""
    root = service.root
    late_id = "77777777-7777-4777-8777-777777777777"
    late_path = root / "Knowledge Base/Records/LateA/_collection.md"
    source = "Knowledge Base/Records/Shared"
    marker = authority.read_marker(root)
    assert call(root, "record_memory", action="append", collection=KEY, item={"title": "Before"},
                item_key=ROW, why="existing file baseline")["outcome"] == "committed"
    baseline = call(root, "record_memory", action="inspect", collection=KEY)

    def arrive():
        late_path.parent.mkdir(parents=True)
        late_path.write_text(manifest_text().replace(CID, late_id).replace("source: Items", "source: " + source))
        (root / source).mkdir()

    with monkeypatch.context() as fault:
        if arrival == "after_discovery":
            original = admission._prepare_create

            def prepare(*args, **kwargs):
                arrive()
                return original(*args, **kwargs)

            fault.setattr(admission, "_prepare_create", prepare)
        else:
            original = admission._CreateAdmission.record

            def record(self, *args, **kwargs):
                result = original(self, *args, **kwargs)
                arrive()
                return result

            fault.setattr(admission._CreateAdmission, "record", record)
        refused(lambda: call(root, "record_memory", action="create", manifest_path=DAILY_PATH,
                             manifest_text=DAILY_TEXT.replace("source: Items", "source: " + source + "/Nested"),
                             why="concurrent file arrival"), "COLLECTION_STORE_MARKER_CONFLICT")
    assert authority.read_marker(root) == marker
    assert not (root / DAILY_PATH).exists() and not (root / source / "Nested").exists()
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        assert authority.pending_create(reader) is None
        assert reader.execute("SELECT collection_id FROM collections ORDER BY collection_id").fetchall() == [(CID,)]
        assert reader.execute("SELECT COUNT(*) FROM txns WHERE collection_id=?", (DAILY,)).fetchone() == (0,)
    after = call(root, "record_memory", action="inspect", collection=KEY)
    assert after["audit"] == baseline["audit"] and after["source_versions"] == baseline["source_versions"]
    assert call(root, "record_memory", action="append", collection=late_id, item={"title": "Arrived file row"},
                item_key=ROW, why="retained file authority")["outcome"] == "committed"
    assert call(root, "record_memory", action="append", collection=KEY, item={"title": "Existing file row"},
                item_key=LATER, why="existing authority")["outcome"] == "committed"
    assert call(root, "record_memory", action="create", manifest_path=DAILY_PATH, manifest_text=DAILY_TEXT,
                why="retry with separate source")["status"] == "committed"
    assert titles(root, late_id) == ["Arrived file row"] and titles(root, KEY) == ["Before", "Existing file row"]


@pytest.mark.parametrize("arrival", ["interrupted", "after_publication"])
def test_pending_create_cannot_publish_over_an_arriving_file_declaration(service, monkeypatch, arrival):
    """Recovery must retain a committed intent instead of seizing a file source that arrived during interruption."""
    root = service.root
    late_id = "77777777-7777-4777-8777-777777777777"
    late_path = root / "Knowledge Base/Records/LateA/_collection.md"
    source = "Knowledge Base/Records/Shared"
    marker = authority.read_marker(root)

    def arrive():
        late_path.parent.mkdir(parents=True)
        late_path.write_text(manifest_text().replace(CID, late_id).replace("source: Items", "source: " + source))
        (root / source).mkdir()

    with monkeypatch.context() as deadline:
        if arrival == "interrupted":
            deadline.setattr(admission, "time", SimpleNamespace(
                monotonic=lambda: time.monotonic() - 31, time=time.time, sleep=time.sleep))
        else:
            original = admission._publish_current_epoch

            def publish(*args, **kwargs):
                result = original(*args, **kwargs)
                if result.status == "published" and not late_path.exists():
                    arrive()
                return result

            deadline.setattr(admission, "_publish_current_epoch", publish)
        created = call(root, "record_memory", action="create", manifest_path=DAILY_PATH,
                       manifest_text=DAILY_TEXT.replace("source: Items", "source: " + source + "/Nested"),
                       why="interrupted create", idempotency_key="arrival-pending")
        assert created["status"] == "committed" and created["warnings"]
        if arrival == "interrupted":
            arrive()
        else:
            assert "COLLECTION_STORE_MARKER_CONFLICT" in created["warnings"][0]
            assert not service.manager._collection_store.reporting_ready(service.manager._fencing_token)
        intent = store_meta(root)[authority.PENDING_CREATE]
    assert call(root, "record_memory", action="append", collection=late_id, item={"title": "File during interruption"},
                item_key=ROW, why="file still authoritative")["outcome"] == "committed"
    assert authority.read_marker(root) == marker and store_meta(root)[authority.PENDING_CREATE] == intent
    assert not (root / DAILY_PATH).exists() and not (root / source / "Nested").exists()
    assert titles(root, late_id) == ["File during interruption"]
    # Repair the overlapping declaration. The pending C intent then uses its original canonical target.
    (root / source).rename(late_path.parent / "Items")
    late_path.write_text(late_path.read_text().replace("source: " + source, "source: Items"))
    _until(lambda: authority.read_marker(root) != marker)
    _until(lambda: authority.PENDING_CREATE not in store_meta(root))
    assert call(root, "record_memory", action="append", collection=DAILY, item={"title": "Recovered C row"},
                item_key=LATER, why="recovered publication")["outcome"] == "committed"
    assert titles(root, late_id) == ["File during interruption"]


def test_accepted_mutation_timeout_retains_late_commit_and_keyed_replay(service, monkeypatch, tmp_path):
    """A lost acknowledgement must neither abandon the live mutation nor replay its leaf."""
    import threading

    monkeypatch.setenv("EXOMEM_LOG_DIR", str(tmp_path / "logs"))

    from exomem import request_budget
    from exomem.collection_store.writer import CollectionWriter

    release, completed = threading.Event(), threading.Event()
    leaf_calls = 0
    append = CollectionWriter.append_record

    def delayed(writer, *args, **kwargs):
        nonlocal leaf_calls
        leaf_calls += 1
        assert release.wait(5), "test did not release the accepted mutation"
        result = append(writer, *args, **kwargs)
        completed.set()
        return result

    monkeypatch.setattr(CollectionWriter, "append_record", delayed)
    arguments = dict(action="append", collection=CID, item={"title": "Late committed"},
                     item_key=LATER, why="late acknowledgement", idempotency_key="late-commit")
    token = request_budget.set_current(request_budget.RequestBudget(
        seconds=request_budget.DELIVERY_RESERVE_SECONDS + 0.3))
    busy_before = busy_total()
    try:
        with pytest.raises(OpError) as pending:
            call(service.root, "record_memory", **arguments)
        assert pending.value.code == "MUTATION_ACKNOWLEDGEMENT_PENDING"
        assert pending.value.as_public_dict()["status"] == "uncertain"
        assert pending.value.as_public_dict()["committed"] is None
        assert "Late committed" not in str(pending.value.as_public_dict())
        # The queue refuses before `invoke` can journal, so the refusal the caller saw is journaled here.
        (refusal,) = [row for row in journal_rows(tmp_path) if row["outcome"] == "failed"]
        assert (refusal["command"], refusal["error_code"]) == ("record_memory", "MUTATION_ACKNOWLEDGEMENT_PENDING")
        assert busy_total() == busy_before  # an uncertain commit is not a MUTATION_BUSY
    finally:
        request_budget.reset_current(token)
        release.set()
    assert completed.wait(5), "the accepted mutation did not complete without a retry"
    summary = service.root / manifest_path().replace("_collection.md", "Items/_summary.md")

    def summary_is_current():
        try:
            return "2 committed rows" in summary.read_text()
        except FileNotFoundError:
            return False

    _until(summary_is_current)
    first = call(service.root, "record_memory", **arguments)
    assert first["outcome"] == "committed"
    assert call(service.root, "record_memory", **arguments) == first
    assert leaf_calls == 1
    assert titles(service.root, CID) == ["Canonical", "Late committed"]


def test_a_store_write_applied_by_the_applier_commits_inside_the_per_intent_bound_not_the_request_budget(
    service, monkeypatch
):
    """The store derives its completion deadline from the budget; an applied intent has no client and must not read as pending.

    The ambient budget here is the one that makes a synchronous call return `MUTATION_ACKNOWLEDGEMENT_PENDING`
    (see the test above); the applier's dispatch lifts it to the per-intent bound.
    """
    import uuid

    from exomem import intent_accept, request_budget
    from exomem.collection_store.writer import CollectionWriter

    release = threading.Event()
    append = CollectionWriter.append_record

    def slow(writer, *args, **kwargs):
        assert release.wait(10), "the slow leaf was never released"
        return append(writer, *args, **kwargs)

    monkeypatch.setattr(CollectionWriter, "append_record", slow)
    threading.Timer(1.0, release.set).start()
    dispatch = intent_accept.ApplierDispatch(
        intent_id="applied-intent", key="applied-late-commit", digest="a" * 64, window_seconds=600.0,
        request_id=str(uuid.uuid4()), stop=threading.Event(),
    )
    token = request_budget.set_current(request_budget.RequestBudget(
        seconds=request_budget.DELIVERY_RESERVE_SECONDS + 0.3))
    try:
        with intent_accept.applier_dispatch(dispatch):
            applied = call(service.root, "record_memory", action="append", collection=CID,
                           item={"title": "Applied late"}, item_key=LATER, why="applied by the applier")
    finally:
        request_budget.reset_current(token)
        release.set()
    assert applied["outcome"] == "committed"
    assert "Applied late" in titles(service.root, CID)


def test_accepted_store_work_can_finish_its_bulk_dependency(service, monkeypatch):
    """A caller waiting for accepted work must not keep that work's bulk dependency parked."""
    from contextlib import contextmanager

    from exomem import foreground_priority

    caller = threading.current_thread()
    queue_wait_finished = threading.Event()
    awaiting_bulk = foreground_priority.awaiting_bulk

    @contextmanager
    def observe_queue_wait():
        with awaiting_bulk():
            yield
        if threading.current_thread() is caller:
            queue_wait_finished.set()

    monkeypatch.setattr(foreground_priority, "awaiting_bulk", observe_queue_wait)

    def finish_bulk_dependency():
        assert queue_wait_finished.wait(5), "the caller did not finish its queue wait"
        with foreground_priority.foreground():
            with foreground_priority.outside_foreground(), foreground_priority.bulk():
                foreground_priority.yield_to_foreground()
                return foreground_priority.foreground_elsewhere()

    server = runtime._server(service.root)
    with foreground_priority.foreground():
        assert server.call(finish_bulk_dependency, wake_maintenance=False) is False


def test_queued_mutation_timeout_never_executes(service, monkeypatch, tmp_path):
    """A request refused before acceptance must leave no later row behind."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setenv("EXOMEM_LOG_DIR", str(tmp_path / "logs"))

    server = runtime._SERVERS[service.root]
    entered, release = threading.Event(), threading.Event()

    def occupy():
        entered.set()
        assert release.wait(5)

    with ThreadPoolExecutor(max_workers=1) as executor:
        occupied = executor.submit(server.call, occupy)
        try:
            assert entered.wait(2)
            # The short budgets belong to the queued request, after setup owns the thread. A write
            # keeps its place past the mutation timeout only while its request budget allows.
            from exomem import request_budget

            token = request_budget.set_current(request_budget.RequestBudget(
                seconds=request_budget.DELIVERY_RESERVE_SECONDS + 0.5))
            busy_before = busy_total()
            try:
                with monkeypatch.context() as queue_timeout, pytest.raises(OpError) as busy:
                    queue_timeout.setattr(service.manager, "_mutation_timeout_seconds", 0.1)
                    call(service.root, "record_memory", action="append", collection=CID,
                         item={"title": "Never accepted"}, item_key=LATER, why="queue timeout")
            finally:
                request_budget.reset_current(token)
            assert busy.value.code == "COLLECTION_STORE_BUSY"
            assert busy.value.details["status"] == "retryable"
            assert busy.value.details["committed"] is False
            diagnostic = busy.value.details["store_thread"]
            assert diagnostic["alive"] is True and diagnostic["queued_requests"] == 0
            assert diagnostic["stack_status"] == "captured" and diagnostic["frames"]
            assert all(set(frame) == {"module", "function", "line"}
                       and frame["module"].startswith("exomem.") for frame in diagnostic["frames"])
            assert str(service.root) not in str(busy.value)
            assert "Never accepted" not in str(busy.value)
            # The queue refuses before `invoke` can journal, so the refusal the caller saw is journaled here.
            (refusal,) = journal_rows(tmp_path)
            assert (refusal["command"], refusal["outcome"], refusal["error_code"]) == (
                "record_memory", "failed", "COLLECTION_STORE_BUSY")
            assert busy_total() == busy_before  # the counter is MUTATION_BUSY's, and this is the store's own code
            # The row is written after the queue releases its lock, which the store thread takes work under.
            holds_queue_lock = []
            with monkeypatch.context() as queue_timeout, pytest.raises(OpError):
                queue_timeout.setattr(service.manager, "_mutation_timeout_seconds", 0.1)
                server.call(lambda: None, on_refusal=lambda *_: holds_queue_lock.append(server._condition._is_owned()))
            assert holds_queue_lock == [False]
        finally:
            release.set()
            occupied.result(timeout=5)
    assert titles(service.root, CID) == ["Canonical"]


def counted_appends(monkeypatch):
    from exomem.collection_store.writer import CollectionWriter

    calls = []
    append = CollectionWriter.append_record

    def counted(writer, *args, **kwargs):
        calls.append(kwargs.get("item_key"))
        return append(writer, *args, **kwargs)

    monkeypatch.setattr(CollectionWriter, "append_record", counted)
    return calls


def test_a_keyed_write_waiting_its_turn_leaves_the_store_thread_free(service, monkeypatch):
    """Defect: a store-routed write that had to wait for its turn behind another write waited on the
    store thread, so a read queued behind it was refused as COLLECTION_STORE_BUSY."""
    from concurrent.futures import ThreadPoolExecutor

    appends = counted_appends(monkeypatch)
    line = service.manager._capture_queue
    ahead = line.enter_line()  # another write already waiting for its turn
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            write = pool.submit(call, service.root, "record_memory", action="append", collection=CID,
                                item={"title": "Waited"}, item_key=LATER, why="waited its turn",
                                idempotency_key="waited-turn")
            _until(lambda: len(line._line) == 2)
            assert titles(service.root, CID) == ["Canonical"]
            line.leave(ahead)
            assert write.result(timeout=20)["outcome"] == "committed"
    finally:
        line.leave(ahead)
    assert appends == [LATER]
    assert titles(service.root, CID) == ["Canonical", "Waited"]


@pytest.mark.parametrize("held", ["at_routing", "after_routing"])
def test_a_keyed_write_waits_out_a_held_boundary_on_its_request_thread(service, monkeypatch, held):
    """Routing proves ownership under the boundary, so a hold met there reached the caller as
    MUTATION_BUSY. A hold met after routing was waited out on the store thread. Both now wait on
    the request thread, and the write commits once."""
    from exomem import mutation_lock

    appends = counted_appends(monkeypatch)
    holding, refusals = threading.Event(), []
    refused = mutation_lock.VaultMutationCoordinator._refused

    def noted(coordinator, wait_start):
        refusals.append(wait_start)
        return refused(coordinator, wait_start)

    monkeypatch.setattr(mutation_lock.VaultMutationCoordinator, "_refused", noted)

    def hold():
        with service.manager.consistency_guard(service.root, operation="test hold"):
            holding.set()
            time.sleep(1.0)

    holder = threading.Thread(target=hold)
    if held == "at_routing":
        holder.start()
        assert holding.wait(5)
    else:
        route = runtime.route

        def route_then_hold(*args):
            server = route(*args)
            if server is not None and not holding.is_set():
                holder.start()
                assert holding.wait(5)
            return server

        monkeypatch.setattr(runtime, "route", route_then_hold)
    try:
        # A short guard bound, so the write meets the hold several times; the service stops with the default.
        with monkeypatch.context() as short:
            short.setattr(service.manager, "_mutation_timeout_seconds", 0.2)
            result = call(service.root, "record_memory", action="append", collection=CID,
                          item={"title": "Waited"}, item_key=LATER, why="held boundary",
                          idempotency_key="held-boundary")
    finally:
        holder.join(10)
    assert result["outcome"] == "committed"
    assert refusals, "the write never met the held boundary"
    assert appends == [LATER]
    assert titles(service.root, CID) == ["Canonical", "Waited"]


@pytest.mark.parametrize("held", ["at_routing", "after_routing"])
def test_a_store_routed_write_refused_at_a_held_boundary_is_journaled_and_counted_once(
        service, monkeypatch, tmp_path, held):
    """The request thread waits out the boundary's busy and finally refuses; the caller sees one
    MUTATION_BUSY, so the journal gets one row and the counter one count. Routing refuses before
    `invoke` runs, and a hold met after routing is refused inside it: each path owes its own row."""
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("EXOMEM_CAPTURE_WAIT_SECONDS", "0.3")
    holding, release = threading.Event(), threading.Event()

    def hold():
        with service.manager.consistency_guard(service.root, operation="test hold"):
            holding.set()
            assert release.wait(10)

    holder = threading.Thread(target=hold)
    if held == "at_routing":
        holder.start()
        assert holding.wait(5)
    else:
        route = runtime.route

        def route_then_hold(*args):
            server = route(*args)
            if server is not None and not holding.is_set():
                holder.start()
                assert holding.wait(5)
            return server

        monkeypatch.setattr(runtime, "route", route_then_hold)
    busy_before = busy_total()
    try:
        with monkeypatch.context() as short, pytest.raises(OpError) as refused:
            short.setattr(service.manager, "_mutation_timeout_seconds", 0.1)
            call(service.root, "record_memory", action="append", collection=CID, item={"title": "Held out"},
                 item_key=LATER, why="held boundary", idempotency_key="held-out")
    finally:
        release.set()
        holder.join(10)

    assert refused.value.code == "MUTATION_BUSY"
    (row,) = journal_rows(tmp_path)
    assert (row["command"], row["outcome"], row["error_code"]) == ("record_memory", "failed", "MUTATION_BUSY")
    assert busy_total() == busy_before + 1


def test_a_store_busy_raised_by_the_work_is_journaled_once(service, monkeypatch, tmp_path):
    """The store thread converts the work's busy for the caller; `invoke` already journaled it, so the queue adds no row."""
    from exomem.collection_store.writer import CollectionWriter

    monkeypatch.setenv("EXOMEM_LOG_DIR", str(tmp_path / "logs"))

    def busy(*_args, **_kwargs):
        raise CollectionStoreError("COLLECTION_STORE_BUSY", "the store is busy")

    monkeypatch.setattr(CollectionWriter, "append_record", busy)
    with pytest.raises(OpError) as refused:
        call(service.root, "record_memory", action="append", collection=CID,
             item={"title": "Busy inside"}, item_key=LATER, why="store busy")

    assert refused.value.code == "COLLECTION_STORE_BUSY"
    (row,) = journal_rows(tmp_path)
    assert (row["command"], row["outcome"], row["error_code"]) == ("record_memory", "failed", "COLLECTION_STORE_BUSY")


def test_queue_time_consumes_the_fixed_read_completion_budget(service, monkeypatch):
    """Waiting for acceptance must not reset the caller's completion deadline.

    A read that misses it changed nothing, so it refuses as retryable; the mutation twin above
    keeps the uncertain code."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from exomem import request_budget

    server = runtime._SERVERS[service.root]
    entered, release, reading, finish = (threading.Event() for _ in range(4))

    def occupy():
        entered.set()
        assert release.wait(5)

    def read():
        reading.set()
        assert finish.wait(5)
        return titles(service.root, CID)

    with monkeypatch.context() as queue_timeout, ThreadPoolExecutor(max_workers=1) as executor:
        queue_timeout.setattr(service.manager, "_mutation_timeout_seconds", 0.7)
        occupied = executor.submit(server.call, occupy)
        assert entered.wait(2)
        timer = threading.Timer(0.5, release.set)
        timer.start()
        entry = time.monotonic()
        token = request_budget.set_current(request_budget.RequestBudget(
            seconds=request_budget.DELIVERY_RESERVE_SECONDS + 0.8, entry=entry))
        try:
            with pytest.raises(OpError) as refused:
                server.call(read, read_only=True)
            assert reading.is_set()
            assert refused.value.code == "COLLECTION_STORE_BUSY"
            assert refused.value.as_public_dict()["status"] == "retryable"
            assert refused.value.as_public_dict()["committed"] is False
            assert refused.value.details["store_thread"]["alive"] is True
            assert 0.75 <= time.monotonic() - entry < 1.15
        finally:
            request_budget.reset_current(token)
            release.set()
            finish.set()
            timer.join(2)
        occupied.result(timeout=5)
    assert titles(service.root, CID) == ["Canonical"]


@contextmanager
def held_handoff(service):
    """The lease manager's store handoff, held open while a store borrower has not yet left: what an idle release does."""
    manager, borrowed, leave = service.manager, threading.Event(), threading.Event()

    def borrow():
        with manager._collection_store_checkout():
            borrowed.set()
            assert leave.wait(10), "the test did not end the handoff"

    borrower = threading.Thread(target=borrow)
    borrower.start()
    assert borrowed.wait(5)
    handoff = threading.Thread(target=manager._release_collection_store, kwargs=dict(
        token=manager._fencing_token, deadline=time.monotonic() + 10, release=False))
    handoff.start()
    try:
        _until(lambda: manager._store_handoff)
        yield leave
    finally:
        leave.set()
        handoff.join(10)
        borrower.join(10)


def test_a_read_arriving_during_a_store_handoff_waits_for_it(service):
    """Defect: every request that arrived while the lease manager drained the store for a handoff,
    which an idle service does about once a minute, was refused as busy."""
    with held_handoff(service) as leave:
        threading.Timer(0.3, leave.set).start()
        assert titles(service.root, CID) == ["Canonical"]


def test_a_read_during_a_store_handoff_that_outlasts_the_mutation_timeout_is_refused_as_busy(service, monkeypatch):
    monkeypatch.setattr(service.manager, "_mutation_timeout_seconds", 0.5)
    with held_handoff(service):
        entry = time.monotonic()
        with pytest.raises(OpError) as busy:
            titles(service.root, CID)
        assert busy.value.code == "COLLECTION_STORE_BUSY" and busy.value.as_public_dict()["committed"] is False
        assert time.monotonic() - entry < 1.5


def test_a_write_during_a_store_handoff_that_outlasts_the_mutation_timeout_commits_once(service, monkeypatch):
    """Defect: a write still waiting when the handoff outlasted the mutation timeout reached the
    store, which refused it as COLLECTION_STORE_BUSY before it ran; an idle release does this about
    once a minute."""
    appends = counted_appends(monkeypatch)
    with held_handoff(service) as leave, monkeypatch.context() as short:
        short.setattr(service.manager, "_mutation_timeout_seconds", 0.5)
        threading.Timer(1.5, leave.set).start()
        result = call(service.root, "record_memory", action="append", collection=CID,
                      item={"title": "After handoff"}, item_key=LATER, why="handoff")
    assert result["outcome"] == "committed"
    assert appends == [LATER]
    assert titles(service.root, CID) == ["After handoff", "Canonical"]


def test_a_keyed_store_write_during_warm_up_does_not_park_the_store_thread(service, monkeypatch):
    """Defect: a keyed write routed to a collection store waited out warm-up on the store thread,
    which never parks, so every other write and read of that store stalled behind it."""
    monkeypatch.setenv("EXOMEM_CAPTURE_WAIT_SECONDS", "3")
    monkeypatch.setattr("exomem.readiness.should_defer", lambda component: component == "semantic_corpus")
    started = time.monotonic()
    with pytest.raises(OpError) as warming:
        call(service.root, "record_memory", action="append", collection=CID, item={"title": "Warm"},
             item_key=LATER, why="during warm-up", idempotency_key="during-warm")
    assert time.monotonic() - started < 1.5
    assert warming.value.code == "MUTATION_WARMING"
    assert warming.value.details["cause"] == "store_thread"
    assert titles(service.root, CID) == ["Canonical"]


def test_a_store_busy_raised_after_the_write_started_reaches_the_caller_once(service, monkeypatch):
    """Only a refusal raised before the write's leaf ran is safe to submit again: an unkeyed write
    submitted again after its leaf ran could write twice."""
    from exomem.collection_store.writer import CollectionWriter

    leaf_calls = []

    def refused_inside(writer, *args, **kwargs):
        leaf_calls.append(kwargs.get("item_key"))
        raise OpError("COLLECTION_STORE_BUSY", "collection store handoff is in progress")

    monkeypatch.setattr(CollectionWriter, "append_record", refused_inside)
    with pytest.raises(OpError) as busy:
        call(service.root, "record_memory", action="append", collection=CID, item={"title": "Once"},
             item_key=LATER, why="inside the leaf")
    assert busy.value.code == "COLLECTION_STORE_BUSY"
    assert leaf_calls == [LATER]


def test_a_clean_service_stop_hands_the_writer_lease_back_with_the_unmoved_head(service, monkeypatch):
    """Defect: uvicorn re-raises the stop signal after a graceful shutdown, so the exit hook never ran
    and the next process waited out the lease TTL. The lifespan end is the only stop this process sees."""
    from exomem import server_runtime

    monkeypatch.setenv("EXOMEM_DISABLE_WARMUP", "1")
    root, client = service.root, service.manager.client
    committed = store_meta(root)[schema.META_COMMIT_SEQ]
    activation = server_runtime.LocalRuntimeActivation(root, fallback_seconds=60.0)

    async def serve_then_stop():
        async with activation.lifespan()(SimpleNamespace()):
            assert client.status().holder == service.manager.config.replica_id

    asyncio.run(serve_then_stop())

    meta = store_meta(root)
    published = json.loads(meta[schema.META_PUBLISHED_REPLICA_HEAD])
    handed_back = client.status()
    assert handed_back.holder is None
    assert meta[schema.META_COMMIT_SEQ] == committed
    assert str(published["commit_seq"]) == committed and schema.META_PENDING_REPLICA_PUBLICATION not in meta
    assert handed_back.collection_store_head.commit_seq == int(committed)


@pytest.mark.parametrize("alias_kind", [
    "symlink", pytest.param("junction", marks=pytest.mark.skipif(
        os.name != "nt", reason="requires native Windows junctions")),
])
def test_projection_admission_accepts_the_served_vault_through_a_root_alias(service, tmp_path, alias_kind):
    """A final-root alias must retain the real store's projection authority."""
    alias = tmp_path / "vault-alias"
    if alias_kind == "junction":
        import subprocess

        subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(service.root)], check=True)
        assert not alias.is_symlink()
    else:
        alias.symlink_to(service.root, target_is_directory=True)
    path = "Knowledge Base/Records/Work/Items/_summary.md"
    with request_scope(OWNER):
        assert egress.release_level_for_path_only(service.root, path) == 6
        assert egress.release_level_for_path_only(alias, path) == 6


@pytest.mark.parametrize("boundary", ["marker", "collections", "knowledge", "unreadable"])
def test_an_unreadable_hint_never_admits_a_canonical_projection_as_an_ordinary_file(service, tmp_path, boundary):
    """The canonical subject stays withheld without its serving writer, regardless of hint accessibility."""
    service.stop()
    root = service.root
    marker = authority.marker_path(root)
    target = marker if boundary in ("marker", "unreadable") else (
        marker.parent if boundary == "collections" else root / "Knowledge Base")
    if boundary == "unreadable":
        if os.geteuid() == 0:
            pytest.skip("root can read mode-zero files")
        target.chmod(0)
    else:
        outside = tmp_path / "detached-ownership"
        target.rename(outside)
        target.symlink_to(outside, target_is_directory=outside.is_dir())
    try:
        with request_scope(OWNER):
            assert egress.release_level_for_path_only(root, "Knowledge Base/Records/Work/Items/_summary.md") == egress.DISCLOSURE_MIN
    finally:
        if boundary == "unreadable":
            target.chmod(0o600)
        else:
            target.unlink()
            outside.rename(target)
