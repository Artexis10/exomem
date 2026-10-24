"""The declared offline migration of file collections into the store, `exomem collections migrate`.

OpenSpec move-structured-collections-to-sqlite P1b.3 and P1b.3b, design §10 and §16 A5.
Each case drives what an owner runs: the CLI with the service stopped and the operator
credential, against the real coordinator app in process, on a generated legacy vault
(both built-ins, both layouts, held files, ``ok``, ``gap`` and ``acknowledged_gap``
chains and a dataset that stays in files). The migration release fence is turned on only
in this temporary state, as the GA release would.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
from contextlib import ExitStack, closing, suppress
from pathlib import Path

import pytest
from record_fixtures import LEDGER_COLLECTION_PATH, copy_dataset_fixture, copy_x3_fixture
from test_collection_store_legacy import _event, _rewrite
from test_collection_store_legacy_import import _held
from test_collection_store_preflight import ledger
from test_collection_store_s1_gate import OLDER, _older, coordinator
from test_collection_store_service import DAILY, DAILY_PATH, DAILY_TEXT, Service, call
from test_collection_store_writer import CID, KEY, OTHER, manifest_path, manifest_text

from exomem import __main__ as cli
from exomem import (
    held_fs,
    record_formats,
    records,
    reserved_paths,
    state_migration,
    state_paths,
    writer_lease,
)
from exomem import structured_collections as collections
from exomem.cli_ops import OpError
from exomem.collection_store import (
    admission,
    authority,
    capability,
    chain,
    connection,
    custody,
    legacy_import,
    migration,
    preflight,
    runtime,
    schema,
    takeover,
)
from exomem.collection_store.connection import CollectionStoreError
from exomem.governance.principal import library_scope
from exomem.init import init_vault

OPERATOR = "EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN"
MIGRATE = frozenset({capability.COLLECTIONS_MIGRATE_V1})
WORK = manifest_path("records")
PLANNING = manifest_path("planning")
LEDGER_ID = "5e9d1e2f-8f0a-4b6c-9d21-6f0f4a2b7c31"
X3_ID = "9ba8d1cf-d1e7-4309-95ae-cb28d7a6eea8"
X3 = "Knowledge Base/Records/Health/X3/_collection.md"
MIGRATED = {CID: WORK, OTHER: PLANNING, LEDGER_ID: LEDGER_COLLECTION_PATH, X3_ID: X3}


def _append_log(root, lines):
    log = root / "Knowledge Base/log.md"
    log.write_text(log.read_text() + "".join(line + "\n" for line in lines))


def legacy_vault(root, *, relationships=False):
    """A file-mode vault as P1b.1 generates them: every shape the migration must carry."""
    init_vault(root)
    ledger(root)  # Records items, filename recipe, real writer history: an `ok` chain
    copy_x3_fixture(root)  # Records log layout
    copy_dataset_fixture(root)  # stays in files
    # Records items with a held file and a `gap` chain: its last event names another manifest.
    work = root / WORK
    work.parent.mkdir(parents=True)
    text = manifest_text("records")
    values = {"title": "One", "count": 3}
    if relationships:
        text = text.replace("item_schema:\n", "item_presentation:\n  version: 1\n  title: title\n"
                            "  relationships: [related]\nitem_schema:\n").replace(
            "    count: {type: integer}\n", "    count: {type: integer}\n    related: {type: link, link_kind: memory}\n")
        target = root / "Knowledge Base/Notes/Reference.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# Reference label\n")
        values["related"] = "[[Reference]]"
    work.write_text(text)
    manifest = collections.load_manifest(root, work)
    (work.parent / "Items").mkdir()
    (work.parent / "Items/Original.md").write_text(
        record_formats.render_markdown_item(manifest, values, KEY, "Authored body\n",
            resolve_relationship=record_formats.presentation_relationship_resolver(root, manifest, None)))
    _held(work, 1, {"action": "append", "item": {"title": "Held"}})
    work.write_text(record_formats.render_manifest_audit_head(
        work.read_text(), f"{3:024x}", semantic_profile="records", reader_version=1))
    manifest = collections.load_manifest(root, work)
    current = record_formats.load_adapter(root, manifest).read().snapshot
    events = [_event(manifest, number) for number in (1, 2, 3)]
    events[-1] = _rewrite(events[-1], after_container_hash=current, after_manifest_hash="f" * 64)
    _append_log(root, reversed(events))
    # Planning items with an acknowledged gap: a rebaseline over an earlier event.
    planning = root / PLANNING
    planning.parent.mkdir(parents=True)
    planning.write_text(manifest_text("planning").replace(CID, OTHER))
    (planning.parent / "Items").mkdir()
    planning.write_text(record_formats.render_manifest_audit_head(
        planning.read_text(), f"{0x102:024x}", semantic_profile="planning", reader_version=2))
    manifest = collections.load_manifest(root, planning)
    rebaseline = records._lifecycle_audit_body(
        transition_id=f"{0x102:024x}", parent_id=f"{0x101:024x}", operation="rebaseline", manifest=manifest,
        before_manifest_hash=manifest.manifest_version.hash, after_manifest_hash=manifest.manifest_version.hash,
        before_container_hash="1" * 64, after_container_hash=record_formats.load_adapter(root, manifest).read().snapshot,
        payload_hash="2" * 64, why="acknowledge invented gap", continuity=False,
        acknowledged_gap_codes=("current-container-mismatch",), gap_fingerprint="3" * 64,
        checkpoint_snapshot_hash="4" * 64)
    _append_log(root, [rebaseline, _rewrite(_event(manifest, 1), transition_id=f"{0x101:024x}")])
    stop_writer()
    return root


def vault_bytes(root, *, outside=None):
    """Every vault file's digest, optionally leaving out one subtree."""
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()
            and (outside is None or not path.is_relative_to(root / outside))}


#: The two query-row fields whose migration change the design's contract table records:
#: the concurrency token (§8 C2) and a migrated log row's ``inferred`` flag (§8 C25).
REPRESENTATION = ("item_version", "inferred")


def rows(root, collection):
    """Each row's content by its key, as the dispatcher answers a query through its profile's tool."""
    tool, identity = ("plan_memory", "plan_id") if collection == PLANNING else ("record_memory", "record_id")
    return {row[identity]: {name: value for name, value in row.items() if name not in REPRESENTATION}
            for row in call(root, tool, action="query", collection=collection)["rows"]}


def stop_writer():
    """Stop this process's writer, as an owner stops the service before an offline apply."""
    manager = writer_lease._MANAGERS.pop(writer_lease.LeaseConfig.from_env(), None)
    if manager is not None:
        manager.close()


def migrate(root, step, *, operator=True):
    """`exomem collections migrate --<step>`, the operator credential set only in the apply shell."""
    with pytest.MonkeyPatch.context() as shell:
        if operator:
            shell.setenv(OPERATOR, "operator")
        return cli._collections_main(["migrate", "--vault", str(root), f"--{step}"])


def printed(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def marker(root):
    return authority.parse_marker(root, authority.read_marker(root))


def fence():
    return writer_lease.LeaseCoordinatorClient(writer_lease.LeaseConfig.from_env()).collection_store_fence()


def staging_directories(root):
    return list(state_paths.vault_state_dir(root).glob(migration.STAGING_PREFIX + "*"))


def start_service(root):
    service = Service(root, writer_lease.start_server_lifecycle())
    from test_collection_store_s1_gate import _until

    _until(lambda: service.manager.status().get("collection_store", {}).get("status") == "admitted")
    return service


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A lease-configured shell over the real coordinator, with the migration fence on."""
    assert Path(connection.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[1] / "src")
    root = tmp_path / "vault"
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv(OPERATOR, raising=False)
    monkeypatch.delenv(custody.SYNC_ROOTS_ENV, raising=False)
    monkeypatch.setattr("exomem.records._capture_sweep_carrier", lambda *a, **kw: None)
    monkeypatch.setattr("exomem.records._due_state_carrier", lambda *a, **kw: None)
    for name, value in {
        "EXOMEM_VAULT_PATH": str(root), "EXOMEM_WRITER_LEASE_URL": "http://localhost",
        "EXOMEM_WRITER_LEASE_VAULT_ID": "migrate", "EXOMEM_WRITER_LEASE_REPLICA_ID": "service",
        "EXOMEM_WRITER_LEASE_TOKEN": "lease", "EXOMEM_WRITER_LEASE_STATE_DIR": str(tmp_path / "lease-service"),
        "EXOMEM_WRITER_LEASE_PREFERRED": "1",
        # Short, so that a crash test waits out a dead owner's lease in seconds.
        "EXOMEM_WRITER_LEASE_TTL": "3",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(capability, "RELEASED", MIGRATE)
    with coordinator(tmp_path / "coordinator.sqlite"), library_scope():
        yield root


def test_offline_migrate_routes_every_records_and_planning_collection_without_changing_a_vault_byte(
        env, capsys):
    """A migration that rewrites or loses a vault file, routes a collection that did not prove,
    blesses a gap, leaves the vault unfenced against old writers or without its compatibility
    descriptor, serves rows that differ from the files it read, or hides migrated Planning
    collections from global inventory."""
    root = legacy_vault(env)

    def histories():
        result = {}
        for cid, path in MIGRATED.items():
            tool = "plan_memory" if path == PLANNING else "record_memory"
            history = call(root, tool, action="query", collection=path,
                           include_agent_history=True)["agent_history"]
            result[cid] = {
                "assessment": {key: value for key, value in history.items() if key != "events"},
                "transition_ids": [event["transition_id"] for event in history["events"]],
            }
        return result

    before = vault_bytes(root)
    expected = {cid: rows(root, path) for cid, path in MIGRATED.items()}
    expected_history = histories()
    report = preflight.preflight(root)
    statuses = {row["collection_id"]: row["legacy_audit_status"] for row in report["collections"]
                if row["status"] == "ready"}
    assert report["verdict"] == "migratable" and set(statuses) == set(MIGRATED)
    assert {statuses[CID], statuses[OTHER], statuses[LEDGER_ID]} == {"gap", "acknowledged_gap", "ok"}
    assert migrate(root, "prepare", operator=False) == 0 and printed(capsys)["staging"] == "retained"
    assert migrate(root, "apply") == 0
    applied = printed(capsys)
    assert (applied["status"], applied["staging"]) == ("marker_admitted", "carried_forward")
    routed = marker(root)
    assert routed["default_authority"] == "file" and routed["version"] == 2
    assert {entry["collection_id"]: entry["manifest_path"] for entry in routed["collections"]} == MIGRATED
    assert {entry["layout"] for entry in routed["collections"]} == {"markdown-items", "markdown-log"}
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        chain.verify_store_chain(reader)
        assert dict(reader.execute("SELECT collection_id,legacy_audit_status FROM collections")) == statuses
        assert reader.execute("SELECT value FROM store_meta WHERE key='store_id'").fetchone()[0] == routed["store_id"]
        assert reader.execute("SELECT 1 FROM store_meta WHERE key=?", (authority.PENDING_CREATE,)).fetchone() is None
    found = fence()
    assert (found.enrolled, found.generation, found.store_id) == (True, 1, routed["store_id"])
    assert {"collections-store-v1", state_migration.COLLECTION_MARKER_COMPATIBILITY_ID} <= set(
        state_migration.recorded_descriptor_ids(root))
    assert vault_bytes(root, outside="Knowledge Base/_Collections") == before
    assert staging_directories(root) == []
    service = start_service(root)
    try:
        assert {cid: rows(root, path) for cid, path in MIGRATED.items()} == expected
        assert histories() == expected_history
        assert call(root, "record_memory", action="inspect", collection=CID)["audit"]["status"] == "gap"
        assert call(root, "record_memory", action="inspect")["unreadable_manifests"] == []
        plans = call(root, "plan_memory", action="inspect")
        assert {entry["collection_id"] for entry in plans["collections"]} == {OTHER}
        assert plans["unreadable_manifests"] == []
    finally:
        service.stop()


def test_a_second_apply_on_a_migrated_vault_reports_nothing_to_migrate(env, capsys):
    """An apply that fails when there is nothing left to migrate, which breaks a script that
    applies and then starts the service on every upgrade."""
    root = legacy_vault(env)
    assert migrate(root, "apply") == 0 and printed(capsys)["status"] == "marker_admitted"
    routed = authority.read_marker(root)
    assert migrate(root, "apply") == 0
    assert printed(capsys)["verdict"] == "nothing_to_migrate" and authority.read_marker(root) == routed


def test_a_failed_proof_leaves_no_store_marker_or_fence_and_names_the_collection_and_check(
        env, capsys, monkeypatch):
    """A migration that publishes a store, a marker or a fence cut although one collection's
    round trip failed, or that does not say which collection and which check."""
    root = legacy_vault(env)
    before = vault_bytes(root)
    original = legacy_import._prove_round_trip

    def drift(captured, *args, **kwargs):
        if captured.manifest.collection_id == LEDGER_ID:
            legacy_import._refuse("b", "render/parse values, body, identity or natural key differ")
        return original(captured, *args, **kwargs)

    monkeypatch.setattr(legacy_import, "_prove_round_trip", drift)
    assert migrate(root, "apply") == 1
    report = printed(capsys)
    blocked = {row["path"]: row for row in report["collections"] if row["status"] == "blocked"}
    assert report["verdict"] == "blocked" and list(blocked) == [LEDGER_COLLECTION_PATH]
    assert blocked[LEDGER_COLLECTION_PATH]["blocker"]["check"] == "b"
    assert not connection.store_path(root).exists() and authority.read_marker(root) is None
    assert (fence().enrolled, fence().generation) == (False, 0)
    assert staging_directories(root) == [] and vault_bytes(root) == before


def test_text_inside_a_legacy_log_block_blocks_the_migration_and_names_the_block(env, capsys):
    """Defect: the first render of a migrated log keeps only each block's values, so a line the
    owner wrote inside a block is lost at the log's first publication, or at an export, which
    renders the same bytes."""
    root = legacy_vault(env)
    log = root / X3.replace("_collection.md", "Training Log.md")
    log.write_bytes(log.read_bytes().replace(b"- Tricep Press | blue short | 17\n",
                                             b"- Tricep Press | blue short | 17\nFelt strong today.\n", 1))
    manifest = collections.load_manifest(root, root / X3)
    [block] = [record.identity.key for record in record_formats.load_adapter(root, manifest).read().records
               if record.values["occurred_on"] == "2024-11-19"]
    before = vault_bytes(root)
    assert migrate(root, "apply") == 1
    report = printed(capsys)
    blocked = {row["path"]: row["blocker"] for row in report["collections"] if row["status"] == "blocked"}
    assert list(blocked) == [X3] and blocked[X3]["check"] == "b" and block in blocked[X3]["reason"]
    assert not connection.store_path(root).exists() and authority.read_marker(root) is None
    assert vault_bytes(root) == before


def _apply_and_crash(environment, database, cut, released):
    """`migrate --apply` in a fresh interpreter that dies at ``cut``, as a killed process would."""
    os.environ.clear()
    os.environ.update(environment)
    records._capture_sweep_carrier = records._due_state_carrier = lambda *a, **kw: None
    capability.RELEASED = released

    from exomem.collection_store import owner, takeover

    def die(*args, **kwargs):
        os._exit(73)

    def after(original):
        def run(*args, **kwargs):
            original(*args, **kwargs)
            os._exit(73)
        return run

    client = writer_lease.LeaseCoordinatorClient
    {"before-install": lambda: setattr(takeover, "replace_live", die),  # after the seal
     "after-install": lambda: setattr(takeover, "replace_live", after(takeover.replace_live)),
     "before-fence": lambda: setattr(admission, "_store_fence", die),
     "after-fence-cas": lambda: setattr(client, "transition_collection_store_fence",
                                        after(client.transition_collection_store_fence)),
     "before-publication": lambda: setattr(admission, "_publish_current_epoch", die),
     # Between the replica publication and the lease renewal that records its head.
     "after-publication": lambda: setattr(admission, "_publish_current_epoch",
                                          after(admission._publish_current_epoch)),
     "before-marker": lambda: setattr(admission, "_install_marker", die),
     "before-fork": lambda: setattr(owner, "record_fork", die),
     "before-staging": lambda: setattr(migration, "_stage", die)}[cut]()
    with coordinator(database), library_scope():
        cli._collections_main(["migrate", "--vault", environment["EXOMEM_VAULT_PATH"], "--apply"])
    os._exit(0)


def _media_authority(environment, database, answer):
    """The media worker's writer-authority probe in a fresh interpreter, as a media child runs it."""
    os.environ.clear()
    os.environ.update(environment)
    from exomem import media_worker

    with coordinator(database):
        answer.put(media_worker._probe_writer_authority())


def wait_out_the_lease():
    """Wait until a failed owner's lease expires, as an owner retrying would."""
    import time

    from test_collection_store_s1_gate import _until

    client = writer_lease.LeaseCoordinatorClient(writer_lease.LeaseConfig.from_env())
    _until(lambda: (client.status().expires_at or 0) < time.time())


def crash_apply(tmp_path, cut, released=MIGRATE):
    """Run one apply that dies at ``cut``, then wait out the dead owner's lease."""
    import multiprocessing

    spawn = multiprocessing.get_context("spawn")
    child = spawn.Process(target=_apply_and_crash, args=(
        {**os.environ, OPERATOR: "operator"}, tmp_path / "coordinator.sqlite", cut, released))
    child.start()
    child.join(120)
    assert child.exitcode == 73
    wait_out_the_lease()


def append_to_work(root, title):
    """A file write to a migrating collection, by the release that serves the vault."""
    manifest = collections.load_manifest(root, root / WORK)
    written = records.append_record(root, WORK, item={"title": title}, why="a write while the store is unadmitted",
                                    expected_container_hash=record_formats.load_adapter(root, manifest).read().snapshot)
    stop_writer()
    return written["item_key"]


def test_a_media_child_gets_writer_authority_on_a_migrated_vault(env, capsys, tmp_path):
    """A media child serves no store. A store fence that admits only a process with a store
    runtime refuses every media child, so media processing stops on a migrated vault."""
    import multiprocessing

    root = legacy_vault(env)
    assert migrate(root, "apply") == 0
    service = start_service(root)
    try:
        token = service.manager.ensure_writer().fencing_token
        spawn = multiprocessing.get_context("spawn")
        answer = spawn.Queue()
        child = spawn.Process(target=_media_authority,
                              args=(dict(os.environ), tmp_path / "coordinator.sqlite", answer))
        child.start()
        child.join(120)
        assert answer.get(timeout=5) is None
        assert service.manager.ensure_writer().fencing_token == token
    finally:
        service.stop()


def test_a_writer_without_a_store_runtime_stays_fenced_until_the_marker_is_installed(env, capsys, tmp_path):
    """A first enrolment cuts the fence before it installs the marker, so no collection routes
    to the store yet. A writer that passes the fence in that window writes the migrating
    collections as files, and the next apply refuses them as stale."""
    root = legacy_vault(env)
    crash_apply(tmp_path, "before-marker")
    with pytest.raises(collections.CollectionError, match="COLLECTION_STORE_UNAVAILABLE"):
        append_to_work(root, "a write between the fence cut and the marker")
    assert migrate(root, "apply") == 0


@pytest.mark.parametrize("stop", ["refused", "killed"])
def test_a_write_after_a_cutover_stops_before_its_fence_cut_reaches_the_store(env, capsys, monkeypatch, tmp_path, stop):
    """A cutover that publishes its store, stops before the fence cut while files are still
    canonical, and on retry admits the stale import, losing a write made in between."""
    root = legacy_vault(env)
    if stop == "refused":
        with monkeypatch.context() as outage:
            outage.setattr(writer_lease.LeaseCoordinatorClient, "transition_collection_store_fence",
                           lambda *args, **kwargs: (_ for _ in ()).throw(OpError(
                               "WRITER_COORDINATOR_UNAVAILABLE", "coordinator unavailable", "retry")))
            assert migrate(root, "apply") == 1
        wait_out_the_lease()
    else:
        crash_apply(tmp_path, "before-fence")
    capsys.readouterr()
    assert connection.store_path(root).exists() and authority.read_marker(root) is None
    assert not fence().enrolled
    with closing(connection.open_head_reader(connection.store_path(root))) as reader:
        intent = authority.pending_create(reader)
        assert intent["kind"] == authority.MIGRATE and intent["expected_marker"] is None
        assert "base_head" not in intent and "supersedes" not in intent
    written = append_to_work(root, "Written while the store waited")
    assert migrate(root, "apply") == 0
    applied = printed(capsys)
    assert (applied["status"], applied["rebuilt_because"], applied["changed"]) == (
        "marker_admitted", migration.BASIS_CHANGED, [WORK])
    service = start_service(root)
    try:
        assert written in rows(root, CID)
    finally:
        service.stop()


ORIGINAL = "Knowledge Base/Records/Work/Items/Original.md"


def edit_by_hand(root):
    """A writer outside the writer lease changes an imported item file, as a sync client or editor would."""
    item = root / ORIGINAL
    item.write_text(item.read_text().replace("Authored body", "Edited by hand"))


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stored_body(root, key):
    """The body the live store holds for one Work item."""
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        return reader.execute("SELECT body FROM items WHERE collection_id=? AND item_key=?", (CID, key)).fetchone()[0]


def foreign_evidence(root):
    """The digests of the replicas a publication kept aside as ``.foreign-*`` evidence."""
    return sorted(sha256(path) for path in (root / "Knowledge Base/_Collections").glob(".foreign-*"))


@pytest.mark.parametrize("cut,relationships", [
    ("before-install", False), ("after-install", False), ("after-fence-cas", False),
    ("after-publication", False), ("after-install", True),
])
def test_a_file_changed_outside_the_lease_after_the_fence_cut_supersedes_the_unadmitted_store(
        env, capsys, tmp_path, cut, relationships):
    """A cutover that admits an import whose files changed after the fence cut, which no writer inside
    the lease could have made, or that stays refused until the owner restores the files by hand; or a
    supersede cut at any step that does not converge, or drops the superseded store's replica."""
    root = legacy_vault(env, relationships=relationships)
    crash_apply(tmp_path, "before-marker")
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        superseded = reader.execute("SELECT value FROM store_meta WHERE key='store_id'").fetchone()[0]
    published = sha256(root / "Knowledge Base/_Collections/collections.sqlite")
    assert (fence().enrolled, fence().generation, fence().store_id) == (True, 1, superseded)
    edit_by_hand(root)
    crash_apply(tmp_path, cut)  # the apply that supersedes the store, cut at one step
    capsys.readouterr()

    assert migrate(root, "apply") == 0

    assert printed(capsys)["status"] == "marker_admitted"
    routed = marker(root)
    assert routed["store_id"] != superseded and routed["collection_store_fence"]["generation"] == 2
    assert (fence().enrolled, fence().generation, fence().store_id) == (True, 2, routed["store_id"])
    assert foreign_evidence(root) == [published]
    assert stored_body(root, KEY) == "Edited by hand\n"


def test_a_supersede_with_another_predecessor_generation_cannot_recover_or_move_the_fence(env, capsys, tmp_path):
    """An interrupted replacement must not use a forged predecessor to pass recovery before its fence CAS."""
    root = legacy_vault(env)
    crash_apply(tmp_path, "before-marker")
    edit_by_hand(root)
    crash_apply(tmp_path, "after-install")
    before = fence()
    with closing(sqlite3.connect(connection.store_path(root))) as writer:
        intent = authority.pending_create(writer)
        assert intent["supersedes"]["generation"] == before.generation
        intent["supersedes"]["generation"] += 1
        writer.execute("UPDATE store_meta SET value=? WHERE key=?",
                       (json.dumps(intent), authority.PENDING_CREATE))
        writer.commit()
        head = chain.verify_store_chain(writer)
    capsys.readouterr()
    assert migrate(root, "apply") == 1
    assert "COLLECTION_STORE_MARKER_CONFLICT" in capsys.readouterr().err
    assert authority.read_marker(root) is None and fence() == before
    with closing(connection.open_head_reader(connection.store_path(root))) as reader:
        assert authority.pending_create(reader) == intent and chain.verify_store_chain(reader) == head


def test_a_crash_after_the_store_is_published_resumes_from_its_intent_without_importing_again(
        env, capsys, monkeypatch, tmp_path):
    """A cutover that stops between publishing the store and installing the marker, then on
    retry imports the files a second time or stays stuck with the store unrouted."""
    root = legacy_vault(env)
    crash_apply(tmp_path, "before-marker")
    assert connection.store_path(root).exists() and authority.read_marker(root) is None
    # Another owner step's preview names the unfinished migration that its apply would meet.
    with pytest.MonkeyPatch.context() as shell:
        shell.setenv(OPERATOR, "operator")
        assert cli._collections_main(["adopt-local", "--vault", str(root), "--why", "check", "--dry-run"]) == 1
    assert "unfinished migration" in capsys.readouterr().err
    imports = []
    monkeypatch.setattr(legacy_import, "import_legacy_collection",
                        lambda *args, **kwargs: imports.append(args) or pytest.fail("imported again"))
    assert migrate(root, "apply") == 0
    resumed = printed(capsys)
    assert (resumed["status"], resumed["staging"], imports) == ("marker_admitted", "resumed", [])
    assert {entry["collection_id"] for entry in marker(root)["collections"]} == set(MIGRATED)
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        assert sorted(cid for (cid,) in reader.execute(
            "SELECT collection_id FROM txns WHERE operation='legacy_import' AND legacy_event_json IS NULL")) == sorted(MIGRATED)
    assert staging_directories(root) == []


def test_a_crash_before_the_store_is_published_discards_its_staging(env, capsys, tmp_path):
    """A cutover that stops before its store goes live, then leaves a fence cut, a store or a
    marker behind, or later publishes the half-sealed staging it left."""
    root = legacy_vault(env)
    before = vault_bytes(root)
    assert migrate(root, "prepare", operator=False) == 0
    capsys.readouterr()
    crash_apply(tmp_path, "before-install")
    assert not connection.store_path(root).exists() and authority.read_marker(root) is None
    assert (fence().enrolled, fence().generation) == (False, 0) and vault_bytes(root) == before
    assert migrate(root, "apply") == 0
    applied = printed(capsys)
    assert (applied["status"], applied["rebuilt_because"]) == ("marker_admitted", migration.INTERRUPTED)
    assert staging_directories(root) == []


def test_staging_whose_basis_lacks_a_field_is_rebuilt(env, capsys):
    """An apply that crashes on, or trusts, staging prepared by a release that recorded less."""
    root = legacy_vault(env)
    assert migrate(root, "prepare", operator=False) == 0
    capsys.readouterr()
    (directory,) = staging_directories(root)
    with closing(sqlite3.connect(directory / "staging.sqlite", isolation_level=None)) as staging:
        (raw,) = staging.execute("SELECT value FROM store_meta WHERE key='migration_basis'").fetchone()
        basis = json.loads(raw)
        for entry in basis["collections"].values():
            del entry["legacy_events"]
        staging.execute("UPDATE store_meta SET value=? WHERE key='migration_basis'", (json.dumps(basis),))
    assert migrate(root, "apply") == 0
    applied = printed(capsys)
    assert (applied["status"], applied["rebuilt_because"]) == ("marker_admitted", migration.NO_BASIS)


def write_from_another_state_dir(monkeypatch, tmp_path, module, name, title, *, root):
    """Make ``module.name`` let one writer that keeps its lease state elsewhere append to Work once.

    Such a writer (a system service beside a user shell) does not share the owner's
    mutation boundary; only the coordinator's writer lease excludes it.
    """
    step, written = getattr(module, name), []

    def then_write(*args, **kwargs):
        found = step(*args, **kwargs)
        if not written:
            errors = []

            def write():
                # Another process has its own authority context, as this thread's fresh context does.
                with library_scope(), pytest.MonkeyPatch.context() as shell:
                    shell.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "other-lease-state"))
                    try:
                        written.append(append_to_work(root, title))
                    except Exception as error:  # noqa: BLE001 - the test thread re-raises the writer's error
                        errors.append(error)

            writer = threading.Thread(target=lambda: contextvars.Context().run(write))
            writer.start()
            writer.join(10)
            assert not writer.is_alive()
            if errors:
                raise errors[0]
        return found

    monkeypatch.setattr(module, name, then_write)
    return written


def test_a_write_from_outside_the_owner_boundary_during_staging_reaches_the_store(env, capsys, monkeypatch, tmp_path):
    """An apply that stages without the writer lease and publishes, losing a write that a writer
    outside its mutation boundary made meanwhile."""
    root = legacy_vault(env)
    assert migrate(root, "prepare", operator=False) == 0
    capsys.readouterr()
    written = write_from_another_state_dir(monkeypatch, tmp_path, migration, "_carried", "Written during staging", root=root)
    assert migrate(root, "apply") == 0
    applied = printed(capsys)
    assert (applied["status"], applied["rebuilt_because"], applied["changed"]) == (
        "marker_admitted", migration.BASIS_CHANGED, [WORK])
    service = start_service(root)
    try:
        assert written[0] in rows(root, CID)
    finally:
        service.stop()


def test_a_write_from_outside_the_owner_boundary_before_a_resume_reaches_the_store(env, capsys, monkeypatch, tmp_path):
    """A resumed cutover that compares the files without the writer lease, then admits an import
    that a writer outside its mutation boundary has changed since."""
    root = legacy_vault(env)
    crash_apply(tmp_path, "before-fence")
    written = write_from_another_state_dir(monkeypatch, tmp_path, migration, "_changed_since_publication",
                                           "Written before the resume", root=root)
    assert migrate(root, "apply") == 0
    assert printed(capsys)["status"] == "marker_admitted"
    service = start_service(root)
    try:
        assert written[0] in rows(root, CID)
    finally:
        service.stop()


def test_a_resume_whose_marker_names_another_store_refuses_before_the_fence_cut(env, capsys, tmp_path):
    """A resumed cutover that cuts the first fence to its store before it finds that the vault's
    marker names another store, which binds every writer to a store it can never admit."""
    root = legacy_vault(env)
    crash_apply(tmp_path, "before-fence")
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        target = authority.pending_create(reader)["target_marker"]
    published = authority.parse_marker(root, target)["store_id"]
    other = target.replace(published, "0b5c8a3e-6d1f-4e2a-9c7b-3f4e5d6a7b8c").encode()
    authority.marker_path(root).parent.mkdir(parents=True, exist_ok=True)
    authority.marker_path(root).write_bytes(other)
    capsys.readouterr()
    assert migrate(root, "apply") == 1
    assert printed(capsys)["status"] == "conflict"
    assert (fence().enrolled, fence().generation) == (False, 0)
    assert authority.read_marker(root) == other


def test_a_file_write_after_prepare_is_imported_at_apply(env, capsys):
    """Carrying a prepared import forward although its collection changed since, which loses
    the write from the store that becomes canonical."""
    root = legacy_vault(env)
    assert migrate(root, "prepare", operator=False) == 0
    capsys.readouterr()
    manifest = collections.load_manifest(root, root / WORK)
    written = records.append_record(root, WORK, item={"title": "Written after prepare"},
                                    why="a write while the standby pre-imports",
                                    expected_container_hash=record_formats.load_adapter(root, manifest).read().snapshot)
    stop_writer()
    assert migrate(root, "apply") == 0
    applied = printed(capsys)
    # Only the written collection changed: the activity-log line the write added leaves the others' basis.
    assert (applied["rebuilt_because"], applied["changed"]) == (migration.BASIS_CHANGED, [WORK])
    service = start_service(root)
    try:
        assert written["item_key"] in rows(root, CID)
    finally:
        service.stop()


@pytest.mark.parametrize("change", ["added", "removed", "identity", "audit"])
def test_refresh_keeps_unchanged_history_when_the_collection_set_or_audit_changes(env, capsys, change):
    """A changed collection set or relevant audit event must not mint another import of unchanged files."""
    root = legacy_vault(env)
    assert migrate(root, "prepare", operator=False) == 0
    capsys.readouterr()
    with closing(connection.open_reader(staging_directories(root)[0] / "staging.sqlite")) as donor:
        history = donor.execute("SELECT receipt_json,legacy_event_json,event_hash FROM txns "
                                "WHERE collection_id=? ORDER BY txn_id", (CID,)).fetchall()
    extra_id = "77777777-7777-4777-8777-777777777777"
    extra_path = "Knowledge Base/Records/Extra/_collection.md"
    if change == "added":
        path = root / extra_path
        (path.parent / "Items").mkdir(parents=True)
        path.write_text(manifest_text("records").replace(CID, extra_id))
    elif change == "removed":
        shutil.rmtree((root / PLANNING).parent)
    elif change == "identity":
        path = root / PLANNING
        path.write_text(manifest_text("planning").replace(CID, extra_id))
    else:
        manifest = collections.load_manifest(root, root / LEDGER_COLLECTION_PATH)
        _append_log(root, [_event(manifest, 40)])
    assert migrate(root, "apply") == 0
    assert printed(capsys)["status"] == "marker_admitted"
    expected = set(MIGRATED)
    if change == "added":
        expected.add(extra_id)
    elif change == "removed":
        expected.remove(OTHER)
    elif change == "identity":
        expected.remove(OTHER)
        expected.add(extra_id)
    with closing(connection.open_reader(connection.store_path(root))) as live:
        assert {row[0] for row in live.execute("SELECT collection_id FROM collections")} == expected
        assert live.execute("SELECT receipt_json,legacy_event_json,event_hash FROM txns "
                            "WHERE collection_id=? ORDER BY txn_id", (CID,)).fetchall() == history
        assert live.execute("PRAGMA foreign_key_check").fetchall() == []
        chain.verify_store_chain(live)


@pytest.mark.parametrize("alteration", ["old", "format", "material", "projection"])
def test_refresh_reproves_ineligible_donors_instead_of_reusing_their_proof(env, capsys, alteration):
    """Old proof formats and altered normalized state must lose eligibility, even with a valid chain."""
    root = legacy_vault(env)
    assert migrate(root, "prepare", operator=False) == 0
    capsys.readouterr()
    path = staging_directories(root)[0] / "staging.sqlite"
    with closing(sqlite3.connect(path, isolation_level=None)) as donor:
        before = dict(donor.execute("SELECT collection_id,receipt_json FROM txns WHERE legacy_event_json IS NULL"))
        (raw,) = donor.execute("SELECT value FROM store_meta WHERE key='migration_basis'").fetchone()
        basis = json.loads(raw)
        if alteration == "old":
            basis["version"] = 1
        elif alteration == "format":
            basis["collections"][LEDGER_ID]["material_version"] = 999
        elif alteration == "material":
            donor.execute("UPDATE items SET governance_json='[]' WHERE collection_id=?", (LEDGER_ID,))
        else:
            donor.execute("UPDATE projection_state SET stat_identity='changed' WHERE collection_id=?", (LEDGER_ID,))
        donor.execute("UPDATE store_meta SET value=? WHERE key='migration_basis'", (json.dumps(basis),))
        chain.verify_store_chain(donor)
    assert migrate(root, "apply") == 0
    assert printed(capsys)["status"] == "marker_admitted"
    with closing(connection.open_reader(connection.store_path(root))) as live:
        after = dict(live.execute("SELECT collection_id,receipt_json FROM txns WHERE legacy_event_json IS NULL"))
        assert after[LEDGER_ID] != before[LEDGER_ID]
        assert (after[CID] == before[CID]) == (alteration != "old")
        assert live.execute("SELECT 1 FROM items WHERE governance_json='[]'").fetchone() is None
        assert live.execute("SELECT 1 FROM projection_state WHERE stat_identity='changed'").fetchone() is None


@pytest.mark.parametrize("replacement", ["basis", "database", "directory", "body", "audit_status"])
def test_seal_rejects_changed_staging_without_publishing_or_removing_evidence(env, capsys, monkeypatch, replacement):
    """A staging replacement after final comparison must not supply new authority for the seal."""
    root = legacy_vault(env)
    assert migrate(root, "prepare", operator=False) == 0
    capsys.readouterr()
    seal = migration._seal
    evidence = []

    def replace(directory, *args, **kwargs):
        path = directory / "staging.sqlite"
        if replacement == "basis":
            with closing(sqlite3.connect(path, isolation_level=None)) as stage:
                stage.execute("UPDATE store_meta SET value=value || ' ' WHERE key='migration_basis'")
            evidence.append(path)
        elif replacement == "database":
            saved = directory / "saved.sqlite"
            path.rename(saved)
            shutil.copyfile(saved, path)
            evidence.extend((saved, path))
        elif replacement == "directory":
            saved = directory.with_name(directory.name + "-saved")
            directory.rename(saved)
            shutil.copytree(saved, directory)
            evidence.extend((saved / "staging.sqlite", path))
        else:
            inode = path.stat().st_ino
            with closing(sqlite3.connect(path, isolation_level=None)) as stage:
                if replacement == "body":
                    stage.execute("UPDATE items SET body='late material change' WHERE collection_id=?", (CID,))
                else:
                    stage.execute("UPDATE collections SET legacy_audit_status='ok' WHERE collection_id=?", (CID,))
            assert path.stat().st_ino == inode
            evidence.append(path)
        return seal(directory, *args, **kwargs)

    monkeypatch.setattr(migration, "_seal", replace)
    assert migrate(root, "apply") == 1
    assert "COLLECTION_STORE_BUSY" in capsys.readouterr().err
    assert not connection.store_path(root).exists() and authority.read_marker(root) is None
    assert all(path.is_file() for path in evidence)
    with closing(connection.open_reader(evidence[-1])) as stage:
        assert authority.pending_create(stage) is None
        assert stage.execute("SELECT 1 FROM store_meta WHERE key='migration_basis'").fetchone() is not None


def test_prepare_stops_at_its_deadline_while_another_migration_holds_staging(env):
    """A standby waiting for staging custody must not consume the finishing reserve or write after expiry."""
    root = legacy_vault(env)
    found = []

    def prepare():
        with library_scope():
            try:
                migration.prepare(root, deadline=time.monotonic() + 0.05)
            except Exception as error:  # noqa: BLE001 - the test thread checks the deadline's error
                found.append(error)

    with reserved_paths._subsystem_authority_scope("collection_store.migration"), \
            reserved_paths._identity_coordination_scope(root, descriptor_ids=(connection.MIGRATION_STAGING,)):
        worker = threading.Thread(target=lambda: contextvars.Context().run(prepare))
        worker.start()
        worker.join(1)
        assert not worker.is_alive()
        assert len(found) == 1 and isinstance(found[0], TimeoutError)
        assert staging_directories(root) == []


@pytest.mark.parametrize("operation", ["prepare", "discard"])
def test_prepare_and_discard_wait_until_the_current_staging_owner_releases_custody(env, capsys, operation):
    """Another prepare or cleanup must not replace or remove a staging file while its owner holds custody."""
    root = legacy_vault(env)
    assert migrate(root, "prepare", operator=False) == 0
    capsys.readouterr()
    path = staging_directories(root)[0] / "staging.sqlite"
    original = path.read_bytes()
    entered, finished = threading.Event(), threading.Event()
    errors = []

    def act():
        with library_scope():
            entered.set()
            try:
                if operation == "prepare":
                    migration.prepare(root)
                else:
                    migration.discard_staging(root)
            except Exception as error:  # noqa: BLE001 - the test thread reports every worker error
                errors.append(error)
            finally:
                finished.set()

    with reserved_paths._subsystem_authority_scope("collection_store.migration"), \
            reserved_paths._identity_coordination_scope(root, descriptor_ids=(connection.MIGRATION_STAGING,)):
        worker = threading.Thread(target=lambda: contextvars.Context().run(act))
        worker.start()
        assert entered.wait(1)
        assert not finished.wait(0.05)
        assert path.read_bytes() == original
    worker.join(5)
    assert not worker.is_alive() and errors == []
    assert not path.exists()


def test_staging_survives_concurrent_comparison_and_cleanup_during_source_reads(env, capsys, monkeypatch):
    """Releasing identity coordination for source reads must neither deadlock nor lose the unfinished candidate."""
    root = legacy_vault(env)
    capture = legacy_import.capture_legacy_collection
    workers, observed, errors = [], [], []

    def inspect():
        with library_scope():
            try:
                directory = staging_directories(root)[0]
                observed.append(migration._carried(root, directory))
                observed.append(migration.discard_staging(root))
                observed.append((directory / "staging.sqlite").is_file())
            except Exception as error:  # noqa: BLE001 - report every worker failure
                errors.append(error)

    def capture_with_competitor(*args, **kwargs):
        if not workers:
            worker = threading.Thread(target=lambda: contextvars.Context().run(inspect))
            workers.append(worker)
            worker.start()
            worker.join(1)
            assert not worker.is_alive(), "a foreign reader must not wait for staging while holding coordination"
            assert errors == []
            assert observed == [(None, migration.NO_BASIS, []), 0, True]
        return capture(*args, **kwargs)

    monkeypatch.setattr(legacy_import, "capture_legacy_collection", capture_with_competitor)
    try:
        assert migrate(root, "apply") == 0
        assert printed(capsys)["status"] == "marker_admitted"
    finally:
        for worker in workers:
            worker.join(6)
            assert not worker.is_alive()


def test_refresh_and_cleanup_preserve_unknown_bytes_from_an_interrupted_attempt(env, capsys):
    """A leftover private file must neither block refresh nor disappear in a blanket directory removal."""
    root = legacy_vault(env)
    assert migrate(root, "prepare", operator=False) == 0
    capsys.readouterr()
    directory = staging_directories(root)[0]
    unknown = directory / "refresh-interrupted.sqlite"
    unknown.write_bytes(b"unrecognized interrupted bytes")
    edit_by_hand(root)
    assert migrate(root, "apply") == 0
    assert printed(capsys)["status"] == "marker_admitted"
    assert unknown.read_bytes() == b"unrecognized interrupted bytes"
    assert migration.discard_staging(root) == 0
    assert unknown.read_bytes() == b"unrecognized interrupted bytes"


@pytest.mark.parametrize("damage", ["old_schema", "unreadable", "wal"])
def test_unusable_staging_reimports_from_files_and_preserves_the_unknown_donor(env, capsys, damage):
    """A stale schema, unreadable store or WAL donor must never become an input to retained assembly."""
    root = legacy_vault(env)
    assert migrate(root, "prepare", operator=False) == 0
    capsys.readouterr()
    donor = staging_directories(root)[0] / "staging.sqlite"
    with ExitStack() as actors:
        if damage == "unreadable":
            donor.write_bytes(b"unreadable retained staging")
        else:
            stage = actors.enter_context(closing(sqlite3.connect(donor, isolation_level=None)))
            if damage == "old_schema":
                stage.execute("UPDATE store_meta SET value=? WHERE key=?",
                              (str(schema.SCHEMA_VERSION - 1), schema.META_SCHEMA_VERSION))
                stage.execute("UPDATE alembic_version SET version_num=?", (str(schema.SCHEMA_VERSION - 1),))
            else:
                assert stage.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
                with held_fs.acquire(donor.parent).require() as filesystem, \
                        filesystem.parent(".").require() as parent, \
                        filesystem.file(parent, donor.name).require() as database:
                    version = database.version()
                    stage.execute("INSERT INTO store_meta(key,value) VALUES('unverified_wal','external write')")
                    assert database.version() == version
                    assert donor.with_name(donor.name + "-wal").is_file()
        damaged = donor.read_bytes()
        assert migrate(root, "apply") == 0
        applied = printed(capsys)
        assert (applied["status"], applied["rebuilt_because"]) == ("marker_admitted", migration.NO_BASIS)
        assert {entry["collection_id"] for entry in marker(root)["collections"]} == set(MIGRATED)
        assert donor.read_bytes() == damaged


@pytest.mark.parametrize("phase", ["commit", "close"])
def test_seal_keeps_sqlite_writer_exclusion_until_the_committed_version_is_captured(env, capsys, monkeypatch, phase):
    """An external write after COMMIT or close must never enter the version that authorizes installation."""
    root = legacy_vault(env)
    assert migrate(root, "prepare", operator=False) == 0
    capsys.readouterr()
    path = staging_directories(root)[0] / "staging.sqlite"
    connect = sqlite3.connect
    blocked = []

    class SealConnection(sqlite3.Connection):
        def external_write(self):
            with closing(connect(path, isolation_level=None, timeout=0)) as external:
                try:
                    external.execute("UPDATE items SET body='write after seal commit' WHERE collection_id=?", (CID,))
                except sqlite3.OperationalError as error:
                    assert error.sqlite_errorcode == sqlite3.SQLITE_BUSY
                    blocked.append(True)
                else:
                    blocked.append(False)

        def execute(self, statement, *args, **kwargs):
            result = super().execute(statement, *args, **kwargs)
            if statement == "COMMIT" and phase == "commit":
                self.external_write()
            return result

        def close(self):
            super().close()
            if phase == "close":
                self.external_write()

    def seal_connection(database, *args, **kwargs):
        if database == f"{path.as_uri()}?mode=rw":
            kwargs["factory"] = SealConnection
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", seal_connection)
    if phase == "commit":
        assert migrate(root, "apply") == 0
        assert printed(capsys)["status"] == "marker_admitted"
        assert blocked and all(blocked)
        with closing(connection.open_reader(connection.store_path(root))) as live:
            assert live.execute("SELECT 1 FROM items WHERE body='write after seal commit'").fetchone() is None
    else:
        assert migrate(root, "apply") == 1
        assert "COLLECTION_STORE_BUSY" in capsys.readouterr().err
        assert blocked and not any(blocked)
        assert path.exists() and not connection.store_path(root).exists() and authority.read_marker(root) is None


@pytest.fixture
def s1(env, tmp_path, monkeypatch, capsys):
    """The legacy vault after S1 enrolled a summary collection, with a row written by the service."""
    monkeypatch.setattr(capability, "RELEASED", MIGRATE | {capability.RECORDS_SUMMARY_V1})
    root = legacy_vault(env)
    (tmp_path / "daily.md").write_text(DAILY_TEXT)
    with pytest.MonkeyPatch.context() as shell:
        shell.setenv(OPERATOR, "operator")
        assert cli._collections_main(["create", "--vault", str(root), "--manifest-path", DAILY_PATH,
                                      "--manifest-file", str(tmp_path / "daily.md"), "--why", "summary"]) == 0
    service = start_service(root)
    try:
        assert call(root, "record_memory", action="append", collection=DAILY, item={"title": "Summary row"},
                    item_key=KEY, why="first row")["outcome"] == "committed"
    finally:
        service.stop()
    capsys.readouterr()
    return root


@pytest.mark.parametrize("existing_store", [False, True])
def test_offline_migration_keeps_its_lease_through_a_slow_final_proof(
        env, capsys, monkeypatch, request, existing_store):
    """A final source proof beyond one lease TTL must preserve authority and every collection."""
    root = request.getfixturevalue("s1") if existing_store else legacy_vault(env)
    expected = {cid: rows(root, path) for cid, path in MIGRATED.items()}
    stop_writer()
    client = writer_lease.LeaseCoordinatorClient(writer_lease.LeaseConfig.from_env())
    capture = legacy_import.capture_legacy_collection
    delayed = False
    owners = []

    def slow_capture(*args, **kwargs):
        nonlocal delayed
        if writer_lease._ACTIVE_WRITE_FENCE.get() is None:
            assert client.status().holder is None
        elif not delayed:
            delayed = True
            owners.append(writer_lease._ACTIVE_WRITE_FENCE.get()[0])
            time.sleep(writer_lease.LeaseConfig.from_env().ttl_seconds + 0.5)
        return capture(*args, **kwargs)

    monkeypatch.setattr(legacy_import, "capture_legacy_collection", slow_capture)
    assert migrate(root, "apply") == 0
    assert printed(capsys)["status"] == "marker_admitted" and delayed
    assert owners[0].status()["renewer_alive"] is False
    assert client.status().holder is None
    service = start_service(root)
    try:
        assert {cid: rows(root, path) for cid, path in MIGRATED.items()} == expected
        if existing_store:
            assert [row["title"] for row in call(root, "record_memory", action="query", collection=DAILY)["rows"]] == [
                "Summary row"]
    finally:
        service.stop()


def test_a_failed_proof_on_an_s1_vault_reports_it_and_leaves_the_store_marker_and_fence(s1, capsys, monkeypatch):
    """An apply on a vault with a live S1 store that fails a proof, then loses the report that
    names the collection and the check behind a lease it cannot hand back."""
    root = s1
    marked = authority.read_marker(root)
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        head = chain.verify_store_chain(reader)
    original = legacy_import._prove_round_trip

    def drift(captured, *args, **kwargs):
        if captured.manifest.collection_id == LEDGER_ID:
            legacy_import._refuse("b", "render/parse values, body, identity or natural key differ")
        return original(captured, *args, **kwargs)

    monkeypatch.setattr(legacy_import, "_prove_round_trip", drift)
    assert migrate(root, "apply") == 1
    blocked = {row["path"]: row["blocker"]["check"] for row in printed(capsys)["collections"]
               if row["status"] == "blocked"}
    assert blocked == {LEDGER_COLLECTION_PATH: "b"}
    assert authority.read_marker(root) == marked
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        assert chain.verify_store_chain(reader) == head


@pytest.mark.parametrize("change", ["database", "body"])
def test_installation_rejects_a_replaced_sealed_source_before_moving_the_live_store(s1, capsys, monkeypatch, change):
    """A replaced or edited source after seal must not move the live store aside or acquire its authority."""
    root = s1
    marked = authority.read_marker(root)
    with closing(connection.open_reader(connection.store_path(root))) as live:
        head = chain.verify_store_chain(live)
    install = takeover.replace_live
    evidence = []

    def replace_source(session, leaf, check, **kwargs):
        directory = session.path.parent / kwargs["subdirectory"]
        path = directory / leaf
        if change == "database":
            saved = directory / "sealed-source.sqlite"
            path.rename(saved)
            shutil.copyfile(saved, path)
            evidence.append(saved)
        else:
            inode = path.stat().st_ino
            with closing(sqlite3.connect(path, isolation_level=None)) as stage:
                stage.execute("UPDATE items SET body='post-seal material change' WHERE collection_id=?", (CID,))
            assert path.stat().st_ino == inode
        evidence.append(path)
        return install(session, leaf, check, **kwargs)

    monkeypatch.setattr(takeover, "replace_live", replace_source)
    assert migrate(root, "apply") == 1
    assert "COLLECTION_STORE_BUSY" in capsys.readouterr().err
    assert authority.read_marker(root) == marked
    with closing(connection.open_reader(connection.store_path(root))) as live:
        assert chain.verify_store_chain(live) == head
    assert all(path.is_file() for path in evidence)


def test_an_s1_store_keeps_its_summary_rows_head_and_fence(s1, capsys):
    """Publishing a fresh store over the vault's live S1 store, which drops its summary
    collection, forks its head or moves its fence, so other hosts diverge."""
    root = s1
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        head = chain.verify_store_chain(reader)
    enrolled = fence()
    assert migrate(root, "prepare", operator=False) == 0 and migrate(root, "apply") == 0
    applied = printed(capsys)
    assert (applied["status"], applied["staging"]) == ("marker_admitted", "carried_forward")
    routed = marker(root)
    assert {entry["collection_id"] for entry in routed["collections"]} == {DAILY, *MIGRATED}
    assert (fence().generation, fence().store_id) == (enrolled.generation, enrolled.store_id) == (
        routed["collection_store_fence"]["generation"], routed["store_id"])
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        assert chain.head_relation(reader, *head) == "ancestor"
    service = start_service(root)
    try:
        assert [row["title"] for row in call(root, "record_memory", action="query", collection=DAILY)["rows"]] == [
            "Summary row"]
    finally:
        service.stop()


def test_a_store_write_after_prepare_forces_a_fresh_copy(s1, capsys):
    """Swapping in a store copied before a later store write, which loses that write."""
    root = s1
    assert migrate(root, "prepare", operator=False) == 0
    service = start_service(root)
    try:
        assert call(root, "record_memory", action="append", collection=DAILY, item={"title": "After prepare"},
                    item_key=OTHER, why="a store write after prepare")["outcome"] == "committed"
    finally:
        service.stop()
    capsys.readouterr()
    assert migrate(root, "apply") == 0
    applied = printed(capsys)
    assert (applied["status"], applied["rebuilt_because"]) == ("marker_admitted", migration.LIVE_CHANGED)
    service = start_service(root)
    try:
        assert sorted(row["title"] for row in call(root, "record_memory", action="query", collection=DAILY)["rows"]) == [
            "After prepare", "Summary row"]
    finally:
        service.stop()


def test_a_writer_without_a_store_runtime_stays_fenced_while_an_s1_migration_is_pending(s1, capsys, tmp_path):
    """On an S1 vault the marker predates the migration and does not yet route the collections
    it imports. A writer that passes the fence while the migration is pending writes those
    collections as files, and the next apply refuses them as stale."""
    root = s1
    crash_apply(tmp_path, "before-marker", released=MIGRATE | {capability.RECORDS_SUMMARY_V1})
    with pytest.raises(collections.CollectionError, match="COLLECTION_STORE_UNAVAILABLE"):
        append_to_work(root, "a write while the migration is pending")
    assert migrate(root, "apply") == 0

S1_RELEASED = MIGRATE | {capability.RECORDS_SUMMARY_V1}


def live_head(root):
    """The live store's instance and head."""
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        instance = reader.execute("SELECT value FROM store_meta WHERE key='instance_id'").fetchone()[0]
        return instance, chain.verify_store_chain(reader)


def summary_titles(root):
    return [row["title"] for row in call(root, "record_memory", action="query", collection=DAILY)["rows"]]


@pytest.mark.parametrize("cut", ["before-publication", "after-publication", "before-marker"])
def test_an_s1_migration_whose_files_changed_after_publication_restores_its_store_and_migrates_again(
        s1, capsys, tmp_path, cut):
    """An apply on an S1 vault that admits an import whose files changed after it published its store,
    or stays refused until the owner restores the files by hand; or a restore that drops the summary
    rows or head, forks although nothing of the migrated store left this host, or keeps the
    coordinator's head on the abandoned import, which every host then refuses to serve."""
    root = s1
    instance, head = live_head(root)
    enrolled = fence()
    crash_apply(tmp_path, cut, released=S1_RELEASED)
    edit_by_hand(root)
    capsys.readouterr()

    assert migrate(root, "apply") == 0

    applied = printed(capsys)
    reached = cut != "before-publication"  # the migrated store reached the replica or the coordinator
    assert (applied["status"], applied["restored"]["forked"]) == ("marker_admitted", reached)
    routed = marker(root)
    assert {entry["collection_id"] for entry in routed["collections"]} == {DAILY, *MIGRATED}
    assert (fence().store_id, fence().generation) == (enrolled.store_id, enrolled.generation)
    tenure, _ = live_head(root)
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        assert chain.head_relation(reader, *head) == "ancestor"
    recorded = writer_lease.LeaseCoordinatorClient(writer_lease.LeaseConfig.from_env()).status().collection_store_head
    assert recorded.instance_id == tenure and (tenure != instance) == reached
    assert stored_body(root, KEY) == "Edited by hand\n"
    service = start_service(root)
    try:
        assert summary_titles(root) == ["Summary row"]
    finally:
        service.stop()


def test_an_s1_restore_cut_before_its_fork_finishes_on_the_next_apply(s1, capsys, tmp_path):
    """A restore killed after it put the superseded store back but before it continued it as a new
    tenure, after which every apply refuses behind the coordinator's head on the abandoned import."""
    root = s1
    _, head = live_head(root)
    crash_apply(tmp_path, "before-marker", released=S1_RELEASED)
    edit_by_hand(root)
    crash_apply(tmp_path, "before-fork", released=S1_RELEASED)
    assert live_head(root)[1] == head  # restored, not yet continued
    from test_collection_store_s1_gate import _until

    service = Service(root, writer_lease.start_server_lifecycle())
    try:
        # Until the fork, takeover refuses the migration's replica, so the restored store commits nothing.
        _until(lambda: service.manager.status().get("collection_store", {}).get("status") in {
            "admitted", "sync_pending"}, seconds=30)
        assert service.manager.status()["collection_store"]["status"] == "sync_pending"
    finally:
        with suppress(OpError):
            service.stop()
    wait_out_the_lease()
    capsys.readouterr()

    assert migrate(root, "apply") == 0

    applied = printed(capsys)
    assert (applied["status"], applied["restored"]["forked"]) == ("marker_admitted", True)
    assert stored_body(root, KEY) == "Edited by hand\n"
    service = start_service(root)
    try:
        assert summary_titles(root) == ["Summary row"]
    finally:
        service.stop()


def refuse_ledger_round_trip(original):
    """The import proof as it fails for one collection whose rendered rows do not parse back."""
    def drift(captured, *args, **kwargs):
        if captured.manifest.collection_id == LEDGER_ID:
            legacy_import._refuse("b", "render/parse values, body, identity or natural key differ")
        return original(captured, *args, **kwargs)
    return drift


@pytest.mark.parametrize("stop", ["blocked", "killed"])
def test_a_restored_store_that_served_a_write_before_its_migration_went_live_migrates_again(
        s1, capsys, monkeypatch, tmp_path, stop):
    """A restore that finished while the new migration never went live (its proof blocked, or the
    apply was killed), after which the restored store served a write: every later apply then reads
    the abandoned migration as an unfinished restore and refuses it as unprovable, for good."""
    root = s1
    crash_apply(tmp_path, "before-publication", released=S1_RELEASED)
    edit_by_hand(root)
    if stop == "blocked":
        with monkeypatch.context() as patched:
            patched.setattr(legacy_import, "_prove_round_trip", refuse_ledger_round_trip(legacy_import._prove_round_trip))
            assert migrate(root, "apply") == 1  # restored, then the new staging's proof blocks
    else:
        crash_apply(tmp_path, "before-staging", released=S1_RELEASED)  # restored, then killed
    wait_out_the_lease()
    service = start_service(root)
    try:
        assert call(root, "record_memory", action="append", collection=DAILY, item={"title": "Later row"},
                    item_key=OTHER, why="a write after the restore")["outcome"] == "committed"
    finally:
        service.stop()
    capsys.readouterr()

    assert migrate(root, "apply") == 0

    assert printed(capsys)["status"] == "marker_admitted"
    assert stored_body(root, KEY) == "Edited by hand\n"
    service = start_service(root)
    try:
        assert sorted(summary_titles(root)) == ["Later row", "Summary row"]
    finally:
        service.stop()


def test_a_service_never_finishes_a_migration_its_owner_left_unfinished(s1, monkeypatch, tmp_path):
    """A serving S1 store that completes a pending migration on its own, without the owner's
    recheck of the files the migration imported."""
    from test_collection_store_s1_gate import _until

    root = s1
    before = authority.read_marker(root)
    crash_apply(tmp_path, "before-marker", released=MIGRATE | {capability.RECORDS_SUMMARY_V1})
    ticks = []
    tick = runtime.StoreServer._create_tick
    # Only a synchronisation point: the assertions below read the vault, not the calls.
    monkeypatch.setattr(runtime.StoreServer, "_create_tick", lambda server: ticks.append(server) or tick(server))
    service = Service(root, writer_lease.start_server_lifecycle())
    try:
        _until(lambda: sum(server.runtime is not None for server in ticks) >= 2)
    finally:
        # A service that never admitted the store has no head to hand off; its lease expires.
        with suppress(OpError):
            service.stop()
    assert authority.read_marker(root) == before
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        assert authority.pending_create(reader)["kind"] == authority.MIGRATE


def test_every_migration_step_refuses_while_the_release_keeps_it_dark(env, capsys, monkeypatch):
    """A release that has not enabled collections-migrate-v1 still moving a vault out of files,
    or touching the lease, the fence or the state directory first."""
    root = legacy_vault(env)
    monkeypatch.setattr(capability, "RELEASED", frozenset())
    for step in ("dry-run", "prepare", "apply"):
        assert migrate(root, step) == 1
        assert capability.MIGRATION_UNAVAILABLE in capsys.readouterr().err
    with pytest.raises(CollectionStoreError, match=capability.MIGRATION_UNAVAILABLE):
        migration.cutover(None, None, None)
    assert staging_directories(root) == [] and not connection.store_path(root).exists()
    assert (fence().enrolled, fence().generation) == (False, 0)


def test_an_install_without_lease_coordination_is_not_migratable_and_names_the_setup(tmp_path, monkeypatch, capsys):
    """A stdio install with no lease coordinator that the preflight calls migratable, whose prepare
    or apply then refuses with an unrelated lease code, or that stages or opens a store first (D7)."""
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("EXOMEM_WRITER_LEASE_URL", raising=False)
    monkeypatch.delenv(OPERATOR, raising=False)
    monkeypatch.setattr("exomem.records._capture_sweep_carrier", lambda *a, **kw: None)
    monkeypatch.setattr("exomem.records._due_state_carrier", lambda *a, **kw: None)
    monkeypatch.setattr(capability, "RELEASED", MIGRATE)
    root = legacy_vault(tmp_path / "vault")
    with library_scope():
        report = preflight.preflight(root)
        assert (report["verdict"], report["blocker"]["code"]) == ("blocked", admission.COORDINATION_REQUIRED)
        assert report["coordinator"] == {"status": "not_configured"}
        for step in ("prepare", "apply"):
            assert migrate(root, step) == 1
            assert admission.COORDINATION_REQUIRED in capsys.readouterr().err
    assert staging_directories(root) == [] and not connection.store_path(root).exists()


def test_a_summary_create_without_lease_coordination_names_the_setup_before_enrolment(tmp_path, monkeypatch):
    """A summary create on an install with no lease coordinator that sends the owner to the enrolment
    command, which cannot run there either, instead of to the coordinator setup (D7)."""
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("EXOMEM_WRITER_LEASE_URL", raising=False)
    monkeypatch.setattr(capability, "RELEASED", frozenset({capability.RECORDS_SUMMARY_V1}))
    root = tmp_path / "vault"
    init_vault(root)
    with pytest.raises(CollectionStoreError) as refused:
        call(root, "record_memory", action="create", manifest_path=DAILY_PATH, manifest_text=DAILY_TEXT,
             why="summary")
    assert refused.value.code == admission.COORDINATION_REQUIRED
    assert not connection.store_path(root).exists()


def test_a_summary_create_on_a_cloud_cell_names_no_remedy_its_tenant_cannot_apply(tmp_path, monkeypatch):
    """A Cloud tenant told to set the lease URL, a variable the cell's manifests forbid, after the
    summary release made the create reach the coordination refusal (D7)."""
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("EXOMEM_WRITER_LEASE_URL", raising=False)
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setattr(capability, "RELEASED", frozenset({capability.RECORDS_SUMMARY_V1}))
    root = tmp_path / "vault"
    init_vault(root)
    with pytest.raises(CollectionStoreError) as refused:
        call(root, "record_memory", action="create", manifest_path=DAILY_PATH, manifest_text=DAILY_TEXT,
             why="summary")
    assert refused.value.code == admission.COORDINATION_REQUIRED
    assert "EXOMEM_WRITER_LEASE_URL" not in refused.value.reason
    assert not connection.store_path(root).exists()


def test_a_coordinator_without_the_store_fence_names_the_release_to_upgrade_to(tmp_path, monkeypatch):
    """A preflight that takes an older coordinator's silence for collections-store-v1 support,
    or refuses without naming the release that serves it (ruled N3)."""
    from io import BytesIO

    from starlette.testclient import TestClient

    from exomem.lease_coordinator import create_app

    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setattr("exomem.records._capture_sweep_carrier", lambda *a, **kw: None)
    monkeypatch.setattr("exomem.records._due_state_carrier", lambda *a, **kw: None)
    root = legacy_vault(tmp_path / "vault")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_URL", "http://localhost")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_VAULT_ID", "migrate")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_REPLICA_ID", "service")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_TOKEN", "lease")
    app = create_app(database=tmp_path / "coordinator.sqlite", bearer_token="lease", operator_token="operator")
    # An older coordinator: the same app before it served the store-fence route.
    app.router.routes = [route for route in app.router.routes
                         if "collection-store-fence" not in getattr(route, "path", "")]
    with closing(TestClient(app)) as transport:
        def urlopen(request, timeout):
            response = transport.request(request.method, request.full_url, content=request.data,
                                         headers=dict(request.header_items()))
            if response.status_code != 200:
                import urllib.error

                raise urllib.error.HTTPError(request.full_url, response.status_code, "absent", {}, BytesIO())
            return BytesIO(response.content)

        monkeypatch.setattr("urllib.request.urlopen", urlopen)
        with library_scope(), pytest.raises(CollectionStoreError) as refused:
            preflight.preflight(root)
    from importlib.metadata import version

    assert refused.value.code == preflight.UPGRADE_REQUIRED
    assert f"upgrade it to Exomem {version('exomem')}" in str(refused.value)


def test_launcher_refuses_an_actual_older_interpreter_on_a_migrated_fresh_root_copy(env, capsys, tmp_path, monkeypatch):
    """The supported launcher handing a migrated vault, including its log-layout collection,
    to a store-blind older release that would write its files as if they were canonical."""
    from exomem.service_manager import WorkerRuntime

    root = legacy_vault(env)
    assert migrate(root, "apply") == 0
    assert {entry["layout"] for entry in marker(root)["collections"]} == {"markdown-items", "markdown-log"}
    copy = tmp_path / "copy"
    shutil.copytree(root, copy)
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "fresh-state"))
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(copy))
    runtime = WorkerRuntime(tmp_path / "worker.sock", host="127.0.0.1", port=8765)
    target = asyncio.run(runtime.inspect({"python": OLDER, "version": _older()}))
    with pytest.raises(ValueError, match="does not support required state compatibility"):
        runtime.migration_required(target)


NESTED = "Knowledge Base/Records/Work/Items/Nested/_collection.md"


@pytest.mark.parametrize("staging", ["fresh", "prepared", "refresh"])
def test_migration_with_relationships_can_read_an_existing_governance_sidecar(env, capsys, staging):
    """Valid governance reads must work for fresh imports, retained staging and refreshed material."""
    from exomem.governance import store

    root = legacy_vault(env, relationships=True)
    store.open_connection(root).close()
    assert migrate(root, "dry-run") == 0
    assert printed(capsys)["verdict"] == "migratable"
    if staging != "fresh":
        assert migrate(root, "prepare", operator=False) == 0
        assert printed(capsys)["staging"] == "retained"
        if staging == "refresh":
            edit_by_hand(root)
    assert migrate(root, "apply") == 0
    applied = printed(capsys)
    assert applied["status"] == "marker_admitted"
    assert (applied["staging"] == "carried_forward") == (staging == "prepared")


def test_a_store_summary_excluded_from_release_does_not_block_relationship_labels(s1, capsys):
    """A migration that refuses a file collection with relationship labels as COLLECTION_NOT_FOUND
    because an unrelated store summary is excluded from release, which leaves the vault unmigratable."""
    root = s1
    (root / "Knowledge Base/_access.yaml").write_text("excluded: [Records/Daily]\n")
    note = root / "Knowledge Base/Notes/Reference.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Reference label\n")
    links = root / "Knowledge Base/Records/Links/_collection.md"
    links_id = "77777777-7777-4777-8777-777777777777"
    links.parent.mkdir(parents=True)
    links.write_text(manifest_text("records").replace(CID, links_id).replace("title: Work", "title: Links").replace(
        "item_schema:\n", "item_presentation:\n  version: 1\n  title: title\n  relationships: [related]\nitem_schema:\n").replace(
        "    count: {type: integer}\n", "    count: {type: integer}\n    related: {type: link, link_kind: memory}\n"))
    manifest = collections.load_manifest(root, links)
    (links.parent / "Items").mkdir()
    (links.parent / "Items/One.md").write_text(record_formats.render_markdown_item(
        manifest, {"title": "One", "related": "[[Reference]]"}, KEY, "Body\n",
        resolve_relationship=record_formats.presentation_relationship_resolver(root, manifest, None)))
    assert migrate(root, "dry-run") == 0
    assert printed(capsys)["verdict"] == "migratable"
    assert migrate(root, "apply") == 0
    assert printed(capsys)["status"] == "marker_admitted"
    assert links_id in {entry["collection_id"] for entry in marker(root)["collections"]}


def test_a_dry_run_blocks_collections_whose_marker_entries_would_overlap(env, capsys):
    """A dry run that calls a vault migratable although two of its collections would share one
    ownership namespace in the marker, which the apply then refuses."""
    root = legacy_vault(env)
    nested = root / NESTED
    (nested.parent / "Items").mkdir(parents=True)
    nested.write_text(manifest_text("records").replace(CID, "77777777-7777-4777-8777-777777777777")
                      .replace("title: Work", "title: Nested"))
    assert migrate(root, "dry-run") == 1
    blocked = {row["path"]: row["blocker"]["code"] for row in printed(capsys)["collections"]
               if row["status"] == "blocked"}
    assert blocked == dict.fromkeys((NESTED, WORK), "COLLECTION_STORE_MARKER_CONFLICT")
