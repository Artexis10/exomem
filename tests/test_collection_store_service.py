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
    admission,
    authority,
    capability,
    connection,
    custody,
    importer,
    runtime,
)
from exomem.collection_store.connection import CollectionStoreError
from exomem.governance.principal import owner_principal, request_scope

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
    """A/B in files, summary C enrolled offline by the owner, then the service started without the operator
    credential, all under a release that enables records-summary-v1."""
    root = tmp_path / "vault"
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("EXOMEM_COLLECTION_STORE_PREVIEW", raising=False)
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
    route refuses SERVICE_ACTIVE or drops the owner's acknowledgement of skipped changes; or an
    inventory lists C's generated view as a file collection."""
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
    monkeypatch.setattr(capability, "RELEASED", frozenset())

    def progress():
        with closing(connection.open_reader(connection.store_path(root))) as reader:
            return reader.execute("SELECT state,last_row_id FROM rollup_definitions WHERE collection_id=?",
                                  (activity,)).fetchone()

    stopped = progress()
    time.sleep(1.0)
    assert stopped[0] == "building" and progress() == stopped


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
