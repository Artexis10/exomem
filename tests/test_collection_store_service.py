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

import functools
import json
import time
from contextlib import closing
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
    _published,
    _until,
    ab_bytes,
    coordinator,
    run_host,
)
from test_collection_store_summary import summary_text
from test_collection_store_writer import CID, manifest_path, manifest_text

from exomem import __main__ as cli
from exomem import commands, record_governance, records, state_migration, writer_lease
from exomem import structured_collections as collections
from exomem.cli_ops import OpError
from exomem.collection_store import (
    authority,
    capability,
    connection,
    custody,
    importer,
    runtime,
)
from exomem.collection_store.connection import CollectionStoreError
from exomem.governance import egress
from exomem.governance.principal import owner_principal, request_scope

OWNER = owner_principal(surface="mcp")
DAILY = "66666666-6666-4666-8666-666666666666"
DAILY_PATH = manifest_path().replace("Work", "Daily")
SOURCE = "Knowledge Base/Evidence/service/export/rows.ndjson"
MAPPING = {"fields": {"title": "title", "count": "count"}}
ON = frozenset({capability.RECORDS_SUMMARY_V1})
OPERATOR = "EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN"
DAILY_TEXT = summary_text(manifest_text().replace(CID, DAILY).replace("title: Work", "title: Daily"))


def cover_import(setitem):
    """The dispatcher's release coverage of record_memory's import selector, which the
    route-binding lane's governance/egress.py change adds; delete this when it merges."""
    selector = ("record_memory", "action")
    setitem(egress._SELECTOR_ADAPTERS[selector], "import", "mutation")
    setitem(egress._SELECTOR_TOMBSTONE_ADAPTERS[selector], "import", "not-applicable")


def call(root, tool, **arguments):
    command = next(command for command in commands.product_commands_for("mcp") if command.name == tool)
    with request_scope(OWNER):
        return writer_lease.invoke_command(command, root, **arguments)


def imported(root, collection=CID, **request):
    return call(root, "record_memory", action="import", collection=collection, import_request=request)


def titles(root, collection):
    return sorted(row["title"] for row in call(root, "record_memory", action="query", collection=collection)["rows"])


def store_meta(root):
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        return dict(reader.execute("SELECT key,value FROM store_meta"))


def refused(work, code=capability.UNAVAILABLE):
    with pytest.raises((OpError, collections.CollectionError, CollectionStoreError)) as error:
        work()
    assert error.value.code == code, error.value


@dataclass
class Service:
    root: Path
    manager: writer_lease.LeaseManager

    def stop(self):
        server = runtime._SERVERS.get(self.root)
        self.manager.close()
        if server is not None:
            server.thread.join(10)
        writer_lease._MANAGERS.pop(self.manager.config, None)


def offline_create(root, manifest_file, path, *, why):
    """The owner's `exomem collections create`, with the operator credential in its own shell."""
    with pytest.MonkeyPatch.context() as shell:
        shell.setenv(OPERATOR, "operator")
        return cli._collections_main(["create", "--vault", str(root), "--manifest-path", path,
                                      "--manifest-file", str(manifest_file), "--why", why])


@pytest.fixture
def service(tmp_path, monkeypatch):
    """A/B in files, C enrolled offline by the owner, then the service started without the operator credential."""
    root = tmp_path / "vault"
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("EXOMEM_COLLECTION_STORE_PREVIEW", raising=False)
    monkeypatch.delenv(OPERATOR, raising=False)
    monkeypatch.delenv(custody.SYNC_ROOTS_ENV, raising=False)
    monkeypatch.setattr("exomem.records._capture_sweep_carrier", lambda *a, **kw: None)
    monkeypatch.setattr("exomem.records._due_state_carrier", lambda *a, **kw: None)
    cover_import(monkeypatch.setitem)
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
    (tmp_path / "c.md").write_text(manifest_text())
    with coordinator(tmp_path / "coordinator.sqlite"):
        assert offline_create(root, tmp_path / "c.md", manifest_path(), why="new store collection") == 0
        started = Service(root, writer_lease.start_server_lifecycle())
        try:
            _until(lambda: started.manager.status().get("collection_store", {}).get("status") == "admitted")
            assert call(root, "record_memory", action="append", collection=CID, item={"title": "Canonical"},
                        item_key=ROW, why="first row")["outcome"] == "committed"
            yield started
        finally:
            started.stop()


@pytest.mark.xfail(strict=True, raises=state_migration.StateCompatibilityUnsupported,
                   reason="s1-A3: compatibility flip (collections-store-v1) for the service's startup gate")
def test_the_service_startup_gate_admits_the_mixed_vault(service):
    """Defect: the installed service refuses to start on a vault whose marker routes C to the store."""
    state_migration.require_vault_state_ready(service.root)


def test_the_service_serves_c_from_its_own_session_and_keeps_a_and_b_in_files(service):
    """Defect: the installed service never opens its store, so C refuses or reads its views as
    files; A/B rows reach the store or C traffic changes A/B bytes; or the in-service owner
    route refuses SERVICE_ACTIVE; or an inventory lists C's generated view as a file collection."""
    root = service.root
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
    inventory = call(root, "record_memory", action="inspect")
    assert {row["collection_id"]: row["committed"] for row in inventory["collections"]} == {KEY: 1, CID: 2}
    assert inventory["unreadable_manifests"] == []
    with request_scope(OWNER):  # no store bound, as in a process that does not serve the vault
        unserved = record_governance.inventory_collections(root)
    assert [row["collection_id"] for row in unserved["collections"]] == [KEY]
    assert [(row["path"], row["error_code"]) for row in unserved["unreadable_manifests"]] == [
        (manifest_path(), "COLLECTION_STORE_UNAVAILABLE")]
    _published(SimpleNamespace(meta=lambda: store_meta(root)))  # the off-ack publisher settles first
    preview = call(root, "maintain_memory", mode="collections-store-adopt-local")
    assert call(root, "maintain_memory", mode="collections-store-adopt-local", apply=True,
                plan_id=preview["plan_id"], why="nothing to adopt")["status"] == "in_sync"


def test_records_summary_v1_gates_summary_create_import_and_query_until_released(
        service, tmp_path, monkeypatch, capsys):
    """Defect: the service creates, imports into or answers a summary collection before the release
    enables records-summary-v1, a disabled summary route answers as empty, the gate also stops C's
    items-mode reads, an unenrolled vault's summary create needs no operator step, or the offline
    create runs beside the service."""
    root = service.root
    write_source(root, ndjson({"title": f"Row {i}", "count": i} for i in range(3)), SOURCE)
    marker = authority.read_marker(root)

    def create_daily(vault=root):
        return call(vault, "record_memory", action="create", manifest_path=DAILY_PATH, manifest_text=DAILY_TEXT,
                    why="summary")

    refused(create_daily)
    refused(lambda: imported(root, mode="preview", source_ref=SOURCE, format="ndjson", mapping=MAPPING))
    assert authority.read_marker(root) == marker and titles(root, CID) == ["Canonical"]
    monkeypatch.setattr(capability, "RELEASED", ON)
    plain = tmp_path / "plain"
    (plain / "Knowledge Base/_Schema").mkdir(parents=True)
    refused(lambda: create_daily(plain), "COLLECTION_STORE_ENROLLMENT_REQUIRED")
    (tmp_path / "daily.md").write_text(DAILY_TEXT)
    assert offline_create(root, tmp_path / "daily.md", DAILY_PATH, why="beside the service") == 1
    assert "COLLECTION_STORE_SERVICE_ACTIVE" in capsys.readouterr().err
    created = create_daily()
    assert authority.selected_entry(root, authority.parse_marker(root, authority.read_marker(root)), DAILY), created
    assert call(root, "record_memory", action="append", collection=DAILY, item={"title": "Daily row"},
                why="summary write")["outcome"] == "committed"
    assert titles(root, DAILY) == ["Daily row"]
    preview = imported(root, DAILY, mode="preview", source_ref=SOURCE, format="ndjson", mapping=MAPPING)
    assert preview["rows"]["valid"] == 3
    monkeypatch.setattr(capability, "RELEASED", frozenset())
    refused(lambda: titles(root, DAILY))
    refused(lambda: call(root, "record_memory", action="inspect", collection=DAILY))
    assert titles(root, CID) == ["Canonical"]


def _finish_after_restart(found, continuation):
    """The restarted service, in a fresh interpreter: nothing here calls run_jobs."""
    capability.RELEASED = ON
    cover_import(dict.__setitem__)
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
