"""S1.2a gate: file Records A, file Planning B and a NEW store collection C in one vault.

Every case drives the real writer lease against a real coordinator database and
the production producer session on a real vault path. Restarts and other hosts
are spawned interpreters with their own state roots; older-reader cases run the
actual older interpreter. Cases owned by a later slice are strict xfails naming
it. No case may let C fall back to files, migrate A/B or expose a half-created C.
"""

import functools
import hashlib
import json
import multiprocessing
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path

import pytest
from test_collection_store_summary import summary_text
from test_collection_store_writer import CID, KEY, OTHER, manifest_path, manifest_text

from exomem import records, state_migration
from exomem.cli_ops import OpError
from exomem.collection_store import (
    admission,
    authority,
    capability,
    connection,
    custody,
    replica,
    schema,
    snapshot,
    takeover,
    tokens,
)
from exomem.collection_store.connection import CollectionStoreError
from exomem.collection_store.preview import preview_store
from exomem.governance.principal import library_scope
from exomem.plan_memory import plan_memory
from exomem.record_memory import record_memory

A_PATH = manifest_path("records").replace("Work", "Legacy")
B_PATH = manifest_path("planning").replace("Work", "Legacy")
ROW = "33333333-3333-4333-8333-333333333333"
LATER = "44444444-4444-4444-8444-444444444444"
ONLY_ON_COPY = "55555555-5555-4555-8555-555555555555"
RELEASED = frozenset({capability.RECORDS_SUMMARY_V1})
OLDER = os.environ.get("EXOMEM_TEST_OLDER_READER_PYTHON", "")


@contextmanager
def coordinator(database):
    from starlette.testclient import TestClient

    from exomem.lease_coordinator import create_app

    app = create_app(database=database, bearer_token="lease", operator_token="operator")
    with closing(TestClient(app)) as transport, pytest.MonkeyPatch.context() as patch:
        def urlopen(request, timeout):
            response = transport.request(request.method, request.full_url, content=request.data,
                                         headers=dict(request.header_items()))
            assert response.status_code == 200, response.text
            return BytesIO(response.content)
        patch.setattr("urllib.request.urlopen", urlopen)
        yield


@dataclass
class Host:
    root: Path
    session: object
    manager: object
    operator: object

    def open(self):
        return admission.open_store(self.session, self.manager, fence_client=self.operator)

    def write_c(self, key, title):
        with preview_store(self.root, self.manager._collection_store) as writer:
            with self.manager.mutation_guard(self.root):
                return writer.append_record(CID, item={"title": title}, item_key=key, why="gate write")

    def read_c(self):
        with preview_store(self.root, self.manager._collection_store):
            return sorted(row["title"] for row in record_memory(self.root, "query", collection=CID)["rows"])

    def write_a(self, title):
        key = str(uuid.uuid4())
        with self.manager.mutation_guard(self.root):
            return record_memory(self.root, "append", collection=KEY, item={"title": title},
                                 item_key=key, why="file write")["outcome"]

    def release(self):
        assert self.manager._release_collection_store(
            self.manager._fencing_token, deadline=time.monotonic() + 30)

    def meta(self):
        with closing(connection.open_reader(self.session.path)) as reader:
            return dict(reader.execute("SELECT key,value FROM store_meta"))


@contextmanager
def host(root, base, name, *, vault_id="gate"):
    from exomem.writer_lease import LeaseConfig, LeaseCoordinatorClient, LeaseManager

    config = LeaseConfig(url="http://localhost", vault_id=vault_id, replica_id=name, token="lease",
                         state_dir=base / f"lease-{name}")
    with admission.production_session(root) as session:
        manager = LeaseManager(config)
        try:
            yield Host(root, session, manager, LeaseCoordinatorClient(replace(config, token="operator")))
        finally:
            manager._stop.set()
            runtime = manager._collection_store
            if runtime is not None and runtime._handle is not None:
                runtime._handle.close()


def _host_main(sender, state, base, name, action, root, database, vault_id):
    """One other host: a fresh interpreter with its own state root, lease state and session."""
    os.environ["EXOMEM_STATE_ROOT"] = str(state)
    # The parent's `ab` fixture patches these; a spawned interpreter applies them again.
    records._capture_sweep_carrier = records._due_state_carrier = lambda *a, **kw: None
    capability.RELEASED = RELEASED
    try:
        with coordinator(database), host(root, base, name, vault_id=vault_id) as found, library_scope():
            sender.send(("ok", action(found)))
    except BaseException as error:  # noqa: BLE001 - reported to the parent assertion
        sender.send(("error", f"{type(error).__name__}: {error}"))
        os._exit(1)
    os._exit(0)


def run_host(base, state, name, action, *, root=None, database=None, vault_id="gate", exit_code=0):
    """Run one host in a spawned interpreter; ``action`` is a module-level function or a partial of one."""
    spawn = multiprocessing.get_context("spawn")
    receiver, sender = spawn.Pipe(duplex=False)
    child = spawn.Process(target=_host_main, args=(
        sender, state, base, name, action, root or base / "vault", database or base / "coordinator.sqlite",
        vault_id))
    child.start()
    sender.close()
    child.join(120)
    try:
        assert not child.is_alive(), "host process did not finish"
        try:
            message = receiver.recv() if receiver.poll(1) else None
        except EOFError:  # a deliberately crashed host sends nothing
            message = None
        assert child.exitcode == exit_code, message
    finally:
        if child.is_alive():
            child.terminate()
            child.join(3)
        receiver.close()
    return None if message is None else message[1]


def file_bytes(root, *subtrees):
    paths = [Path(root) / subtree for subtree in subtrees] or [Path(root)]
    return {path.relative_to(root): path.read_bytes()
            for base in paths for path in sorted(base.rglob("*")) if path.is_file()}


def ab_bytes(root):
    return file_bytes(root, Path(A_PATH).parent, Path(B_PATH).parent)


def create_c(found, **changes):
    arguments = dict(why="new store collection", request_id="create-c", scaffold=True,
                     fence_client=found.operator)
    arguments.update(changes)
    return admission.create_new(found.session, found.manager, manifest_path(), summary_text(), **arguments)


def refused(call, code):
    with pytest.raises((CollectionStoreError, OpError)) as error:
        call()
    assert code in str(error.value)
    return str(error.value)


@pytest.fixture
def ab(tmp_path, monkeypatch):
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv(custody.SYNC_ROOTS_ENV, raising=False)
    monkeypatch.setattr("exomem.records._capture_sweep_carrier", lambda *a, **kw: None)
    monkeypatch.setattr("exomem.records._due_state_carrier", lambda *a, **kw: None)
    # Only a release that enables the slice may enrol a vault in the store.
    monkeypatch.setattr(capability, "RELEASED", RELEASED)
    root = tmp_path / "vault"
    (root / "Knowledge Base").mkdir(parents=True)
    (root / "Knowledge Base/log.md").write_text("# Activity\n")
    # C is a summary collection: its routes need the owner, here the in-process owner.
    with coordinator(tmp_path / "coordinator.sqlite"), host(root, tmp_path, "host-a") as found, library_scope():
        for path, profile, cid in ((A_PATH, "records", KEY), (B_PATH, "planning", OTHER)):
            records.create_collection(root, path, manifest_text(profile).replace(CID, cid),
                                      why="file fixture", scaffold=True)
        yield found


@pytest.fixture
def abc(ab):
    assert create_c(ab)["status"] == "marker_admitted"
    assert ab.write_c(ROW, "Canonical")["outcome"] == "committed"
    return ab


# --- slice 1: production session, custody and create ---------------------------------


def test_abc_reads_guarded_writes_and_audit_follow_each_authority(abc):
    """Defect: a production-created C writes into files, or A/B rows are diverted into the store."""
    root = abc.root
    with preview_store(root, abc.manager._collection_store):
        for call, cid, key_name, append in ((record_memory, KEY, "item_key", "append"),
                                            (plan_memory, OTHER, "plan_id", "add"),
                                            (record_memory, CID, "item_key", "append")):
            added = call(root, append, collection=cid, item={"title": "One"}, **{key_name: LATER},
                         why="capture")
            changed = call(root, "update", collection=cid, **{key_name: LATER}, changes={"title": "Two"},
                           why="correct", expected_container_hash=added["after_container_hash"],
                           expected_item_version=added["after_item_hash"])
            assert changed["outcome"] == "committed"
            assert "Two" in [row["title"] for row in call(root, "query", collection=cid)["rows"]]
            assert call(root, "inspect", collection=cid)["audit"]["status"] == "ok", cid
    with closing(connection.open_reader(abc.session.path)) as reader:
        assert reader.execute("SELECT collection_id FROM collections").fetchall() == [(CID,)]
        assert reader.execute("SELECT count(*) FROM items").fetchone() == (2,)
    for path in (A_PATH, B_PATH):
        assert "Two" in (root / path).parent.joinpath("Items", f"{LATER}.md").read_text()


def test_precommit_failure_leaves_no_c_scaffold_marker_or_fence(ab, monkeypatch):
    """Defect: a rejected canonical commit still publishes C's marker, scaffold or fence in a real vault."""
    before = file_bytes(ab.root)
    prepare = admission._prepare_create

    def reject_commit(writer, *args, **kwargs):
        writer.connection.set_authorizer(lambda action, arg, *_: sqlite3.SQLITE_DENY
                                        if action == sqlite3.SQLITE_TRANSACTION and arg == "COMMIT"
                                        else sqlite3.SQLITE_OK)
        return prepare(writer, *args, **kwargs)

    monkeypatch.setattr(admission, "_prepare_create", reject_commit)
    with pytest.raises(sqlite3.DatabaseError):
        create_c(ab)
    assert file_bytes(ab.root) == before
    assert not authority.marker_path(ab.root).exists()
    assert not (ab.root / manifest_path()).parent.exists()
    assert not ab.operator.collection_store_fence().enrolled


def test_a_production_create_refuses_an_items_mode_collection(ab):
    """Defect: the production producer enrols an items-mode collection in the store, which S1 keeps in files."""
    before = file_bytes(ab.root)
    refused(lambda: admission.create_new(ab.session, ab.manager, manifest_path(), manifest_text(), why="items",
                                         request_id="create-items", fence_client=ab.operator),
            "COLLECTION_STORE_SUMMARY_REQUIRED")
    assert file_bytes(ab.root) == before and not authority.marker_path(ab.root).exists()
    assert not ab.operator.collection_store_fence().enrolled


def _crash_around_cutover(cut, found):
    rename = found.session.fs.rename

    def cut_marker(source, parent, leaf, **kwargs):
        if leaf != "mode.json":
            return rename(source, parent, leaf, **kwargs)
        if cut == "after":
            rename(source, parent, leaf, **kwargs)
        os._exit(73)

    found.session.fs.rename = cut_marker
    create_c(found)


def _resume_create(found):
    return admission.resume_local(found.session, found.manager, fence_client=found.operator)


@pytest.mark.parametrize("cut", ["before", "after"])
def test_crash_around_marker_cutover_recovers_the_same_create_once(ab, cut, tmp_path):
    """Defect: a restarted process re-executes C's create, loses its receipt or leaves C half-routed."""
    before = ab_bytes(ab.root)
    run_host(tmp_path, tmp_path / "state", "host-a", functools.partial(_crash_around_cutover, cut), exit_code=73)
    with closing(connection.open_reader(ab.session.path)) as reader:
        intent = authority.pending_create(reader)
        assert reader.execute("SELECT count(*) FROM txns").fetchone() == (1,)
    assert intent is not None
    marker = authority.read_marker(ab.root)
    assert marker == (None if cut == "before" else intent["target_marker"].encode())
    assert not (ab.root / manifest_path()).exists()

    result = run_host(tmp_path, tmp_path / "state", "host-a", _resume_create)
    assert result["status"] == "marker_admitted"
    assert result["txn_id"] == intent["txn_id"]
    with closing(connection.open_reader(ab.session.path)) as reader:
        assert reader.execute("SELECT count(*) FROM txns").fetchone() == (1,)
        assert authority.pending_create(reader) is None
    assert authority.read_marker(ab.root) == intent["target_marker"].encode()
    assert (ab.root / manifest_path()).exists()
    assert ab_bytes(ab.root) == before


@pytest.mark.parametrize("evidence", ["configured_sync_root", "sync_client_metadata", "windows_sync_folder"])
def test_unverified_custody_refuses_the_c_producer_and_leaves_a_b_usable(abc, evidence, monkeypatch, tmp_path):
    """Defect: a synced or overlapping deployment mints store custody, or refusing it blocks A/B writes."""
    root = abc.root
    if evidence == "configured_sync_root":
        monkeypatch.setenv(custody.SYNC_ROOTS_ENV, str(tmp_path))
    elif evidence == "sync_client_metadata":
        (abc.root / ".stfolder").mkdir()
    else:
        # A Windows-mounted OneDrive folder keeps no client metadata in the tree.
        root = tmp_path / "OneDrive - Contoso" / "vault"
        (root / "Knowledge Base").mkdir(parents=True)
        monkeypatch.setattr(custody, "_windows_mounts", lambda: [tmp_path])
    marker = authority.read_marker(abc.root)

    def mint():
        with admission.production_session(root):
            pass

    refused(mint, "COLLECTION_STORE_CUSTODY_UNVERIFIED")
    assert abc.write_a("File row while C waits") == "committed"
    assert authority.read_marker(abc.root) == marker


def test_custody_lost_before_cutover_keeps_create_pending_until_resumed(ab, monkeypatch):
    """Defect: a sync client appearing mid-create still lets the producer install C's marker."""
    publish = replica.publish_replica

    def sync_appears(*args, **kwargs):
        result = publish(*args, **kwargs)
        (ab.root / ".stfolder").mkdir(exist_ok=True)
        return result

    with monkeypatch.context() as patched:
        patched.setattr(replica, "publish_replica", sync_appears)
        refused(lambda: create_c(ab), "COLLECTION_STORE_CUSTODY_REQUIRED")
    assert not authority.marker_path(ab.root).exists()
    assert not (ab.root / manifest_path()).exists()
    with closing(connection.open_reader(ab.session.path)) as reader:
        assert authority.pending_create(reader)["request_id"] == "create-c"
    (ab.root / ".stfolder").rmdir()
    assert admission.resume_local(ab.session, ab.manager, fence_client=ab.operator)["status"] == "marker_admitted"


def test_custody_lost_after_admission_refuses_the_next_publication(abc):
    """Defect: a vault that became synced after admission still receives C's replica, or loses A/B or C reads."""
    published = replica.replica_path(abc.root).read_bytes()
    (abc.root / ".stfolder").mkdir()
    refused(abc.release, "COLLECTION_STORE_CUSTODY_UNVERIFIED")
    assert replica.replica_path(abc.root).read_bytes() == published
    assert abc.manager.status()["collection_store"]["status"] == "custody_unverified"
    assert abc.read_c() == ["Canonical"]
    assert abc.write_a("A while C waits") == "committed"


def _older():
    assert OLDER, "the gate runner provides EXOMEM_TEST_OLDER_READER_PYTHON"
    return subprocess.run([OLDER, "-I", "-c", "from importlib.metadata import version; print(version('exomem'))"],
                          capture_output=True, text=True, check=True).stdout.strip()


def test_launcher_refuses_an_actual_older_interpreter_on_a_fresh_root_copy(abc, tmp_path, monkeypatch):
    """Defect: the supported launcher hands a C vault copy to a store-blind older release."""
    import asyncio

    from exomem.service_manager import WorkerRuntime

    copy = tmp_path / "copy"
    shutil.copytree(abc.root, copy)
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "fresh-state"))
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(copy))
    runtime = WorkerRuntime(tmp_path / "worker.sock", host="127.0.0.1", port=8765)
    target = asyncio.run(runtime.inspect({"python": OLDER, "version": _older()}))
    with pytest.raises(ValueError, match="does not support required state compatibility"):
        runtime.migration_required(target)
    started = subprocess.run(
        [OLDER, "-I", "-c", "import sys; from pathlib import Path; from exomem import state_migration;"
         " state_migration.require_vault_state_ready(Path(sys.argv[1]))", str(copy)],
        capture_output=True, text=True, env={**os.environ, "EXOMEM_STATE_ROOT": str(tmp_path / "old-state")})
    assert started.returncode != 0
    assert "StateCompatibilityUnsupported" in started.stderr or "COLLECTION_STORE_MARKER_CONFLICT" in started.stderr
    assert not (tmp_path / "old-state").exists() or not any((tmp_path / "old-state").rglob("collections.sqlite"))


def test_older_interpreter_refuses_the_newer_store_schema(abc):
    """Defect: an older release reads C rows from a store schema it cannot interpret."""
    _older()
    opened = subprocess.run(
        [OLDER, "-I", "-c", "import sys; from pathlib import Path;"
         " from exomem.collection_store import connection; connection.open_reader(Path(sys.argv[1]))",
         str(abc.session.path)], capture_output=True, text=True)
    assert opened.returncode != 0 and "COLLECTION_STORE_SCHEMA_NEWER" in opened.stderr


# --- slice 2: restart, takeover, lag, divergence and copied-vault adoption ---------------


def _restart(found):
    return found.open(), found.write_c(LATER, "After restart")["outcome"], found.meta()


def test_restart_reacquires_its_own_head_without_waiting(abc, tmp_path):
    """Defect: a restarted host waits on, re-adopts or refuses its own recorded head."""
    instance = abc.meta()[schema.META_INSTANCE_ID]
    abc.release()
    status, outcome, meta = run_host(tmp_path, tmp_path / "state", "host-a", _restart)
    assert status == {"status": "admitted"} and outcome == "committed"
    assert meta[schema.META_INSTANCE_ID] == instance


def _handoff_on_b(found):
    status = found.open()
    rows = found.read_c()
    found.write_c(LATER, "From B")
    meta = found.meta()
    found.release()
    return status, rows, meta


def test_lease_handoff_adopts_the_newer_replica_in_both_directions(abc, tmp_path):
    """Defect: a new holder writes on its stale local store instead of adopting the recorded head."""
    first = abc.meta()[schema.META_INSTANCE_ID]
    before = ab_bytes(abc.root)
    abc.release()
    status, rows, meta = run_host(tmp_path, tmp_path / "state-b", "host-b", _handoff_on_b)
    assert status == {"status": "admitted"} and rows == ["Canonical"]
    lineage = json.loads(meta[schema.META_LINEAGE])
    assert first in {entry["instance_id"] for entry in lineage}
    assert meta[schema.META_INSTANCE_ID] not in {first}
    stale = abc.session.path.with_name(abc.session.path.name + takeover.SUPERSEDED + "0" * 16)
    stale.write_bytes(b"an older superseded store")
    assert abc.write_c(KEY, "Back on A")["outcome"] == "committed"
    superseded = list(abc.session.path.parent.glob("*" + takeover.SUPERSEDED + "*"))
    assert len(superseded) == 1 and superseded != [stale]
    assert abc.read_c() == ["Back on A", "Canonical", "From B"]
    assert meta[schema.META_INSTANCE_ID] in {entry["instance_id"] for entry in json.loads(
        abc.meta()[schema.META_LINEAGE])}
    assert ab_bytes(abc.root) == before


def _held_read(found):
    """Read C while another thread holds a checkout registration on the same store.

    A checkout's connection belongs to its opening thread, so the other borrower holds
    the manager's checkout registration, which every checkout takes first.
    """
    held, done = threading.Event(), threading.Event()

    def hold():
        with found.manager._collection_store_checkout():
            held.set()
            done.wait(30)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert held.wait(30), "the borrowing thread never checked out"
        return found.read_c()
    finally:
        done.set()
        thread.join(30)


def _adopt_and_release(found):
    assert found.open() == {"status": "admitted"}
    found.release()


def _behind(found):
    pending = found.open()
    takeover.RECHECK_SECONDS = 0  # every checkout re-resolves while another thread borrows
    rows = _held_read(found)
    refusal = refused(lambda: found.write_c(LATER, "Lost write"), "COLLECTION_STORE_SYNC_PENDING")
    with preview_store(found.root, found.manager._collection_store):
        file_write = found.write_a("A while C waits")
    found.release()
    found.write_a("A on a later token")
    found.release()  # a later token than the resolving one still hands the lease back
    return pending, rows, refusal, file_write


def _delivery(delivered, found):
    takeover.ATTENTION_SECONDS = 0
    found.open()
    attention = found.manager.status()["collection_store"]["attention"]
    replica.replica_path(found.root).write_bytes(delivered)
    return attention, found.write_c(LATER, "After delivery")["outcome"], found.read_c()


def test_lagging_replica_is_sync_pending_with_reads_until_delivery(abc, tmp_path):
    """Defect: a holder behind the recorded head writes over it, blocks reads, A/B or the lease, or never rechecks."""
    abc.release()
    run_host(tmp_path, tmp_path / "state-b", "host-b", _adopt_and_release)
    lagging = replica.replica_path(abc.root).read_bytes()
    assert abc.write_c(KEY, "Newer on A")["outcome"] == "committed"
    # B handed back the head A already had: A records its tenure in place, no re-adoption.
    assert not list(abc.session.path.parent.glob("*" + takeover.SUPERSEDED + "*"))
    abc.release()
    delivered = replica.replica_path(abc.root).read_bytes()
    replica.replica_path(abc.root).write_bytes(lagging)
    pending, rows, refusal, file_write = run_host(tmp_path, tmp_path / "state-b", "host-b", _behind)
    assert pending["status"] == "sync_pending"
    assert (pending["recorded_commit_seq"], pending["local_commit_seq"]) == (3, 2)
    assert rows == ["Canonical"]
    assert "adopt-local" in refusal and file_write == "committed"
    assert abc.write_a("A while B waits") == "committed"
    abc.release()
    attention, outcome, final = run_host(tmp_path, tmp_path / "state-b", "host-b",
                                         functools.partial(_delivery, delivered))
    assert attention is True
    assert outcome == "committed" and final == ["After delivery", "Canonical", "Newer on A"]


def _write_and_crash(found):
    assert found.open() == {"status": "admitted"}
    found.write_c(LATER, "Unpublished on B")
    os._exit(73)


def _restart_diverged(found):
    status = found.open()
    rows = found.read_c()
    refused(lambda: found.write_c(OTHER, "Refused"), "COLLECTION_STORE_DIVERGED")
    with preview_store(found.root, found.manager._collection_store):
        file_write = found.write_a("A beside divergence")
    found.manager.close()
    return status, rows, file_write, found.meta()


def test_fork_is_durably_diverged_with_reads_and_a_b_writes_continuing(abc, tmp_path):
    """Defect: a host whose unpublished commits fork from the replica adopts over them or keeps writing."""
    abc.release()
    run_host(tmp_path, tmp_path / "state-b", "host-b", _write_and_crash, exit_code=73)
    stranded = abc.manager.client.status()
    abc.manager.client.release_holder("host-b", stranded.fencing_token)
    assert abc.write_c(KEY, "A after fork")["outcome"] == "committed"
    abc.release()
    status, rows, file_write, meta = run_host(tmp_path, tmp_path / "state-b", "host-b", _restart_diverged)
    assert status["status"] == "diverged" and rows == ["Canonical", "Unpublished on B"]
    assert file_write == "committed" and schema.META_REPLICA_DIVERGENCE in meta
    # The diverged host hands the lease back without replacing the recorded head.
    recorded = abc.manager.client.status().collection_store_head
    assert recorded.instance_id == abc.meta()[schema.META_INSTANCE_ID]
    assert abc.read_c() == ["A after fork", "Canonical"]


def _adopt_late_delivery(delivered, found):
    pending = found.open()
    replica.replica_path(found.root).write_bytes(delivered)
    return pending, found.open(), found.read_c(), found.write_c(LATER, "On the copy")["outcome"]


def test_copied_vault_on_fresh_state_and_coordinator_adopts_the_replica(abc, tmp_path):
    """Defect: a copied vault falls back C to files, imports A/B, cannot write C, or strands a late replica."""
    abc.release()
    copy = tmp_path / "copy"
    shutil.copytree(abc.root, copy)
    original, copied_ab = file_bytes(abc.root), ab_bytes(copy)
    delivered = replica.replica_path(copy).read_bytes()
    _truncated(copy)
    pending, status, rows, outcome = run_host(
        tmp_path, tmp_path / "state-copy", "copy-host", functools.partial(_adopt_late_delivery, delivered), root=copy,
        database=tmp_path / "copy-coordinator.sqlite", vault_id="copy")
    assert pending["status"] == "sync_pending"
    assert status == {"status": "admitted"} and rows == ["Canonical"] and outcome == "committed"
    assert file_bytes(abc.root) == original
    assert ab_bytes(copy) == copied_ab


def _foreign_store(copy):
    with closing(sqlite3.connect(replica.replica_path(copy))) as conn:
        conn.execute("UPDATE store_meta SET value=? WHERE key='store_id'", (str(uuid.uuid4()),))
        conn.commit()


def _missing_marker_collection(copy):
    marker = json.loads(authority.read_marker(copy))
    marker["collections"].append(authority.marker_entry(
        LATER, manifest_path().replace("Work", "Later"), marker["store_id"], "records",
        "Knowledge Base/Records/Later/Items", "markdown-items"))
    authority.marker_path(copy).write_text(json.dumps(marker))


def _truncated(copy):
    path = replica.replica_path(copy)
    path.write_bytes(path.read_bytes()[: path.stat().st_size // 2])


def _foreign_lineage(vault):
    instance = str(uuid.uuid4())
    lineage = [{"instance_id": instance, "adopted_from": None, "adopted_at_commit_seq": 0, "head_hash": None}]
    with closing(sqlite3.connect(replica.replica_path(vault))) as conn:
        conn.execute("UPDATE store_meta SET value=? WHERE key=?", (instance, schema.META_INSTANCE_ID))
        conn.execute("UPDATE store_meta SET value=? WHERE key=?", (json.dumps(lineage), schema.META_LINEAGE))
        conn.commit()


def _granted_epoch(vault):
    with closing(sqlite3.connect(replica.replica_path(vault))) as conn:
        conn.execute("UPDATE store_meta SET value='1000000' WHERE key=?", (schema.META_LEASE_EPOCH,))
        conn.commit()


def _refused_replica(found):
    status = found.open()
    refused(lambda: record_memory(found.root, "query", collection=CID), "")
    return status, found.session.path.exists(), found.write_a("A beside the refused replica")


@pytest.mark.parametrize(("tamper", "copied", "expected"), [
    (_foreign_store, True, "diverged"), (_missing_marker_collection, True, "sync_pending"),
    (_foreign_lineage, False, "diverged"), (_granted_epoch, False, "diverged"),
])
def test_unverified_replica_is_never_adopted(abc, tmp_path, tamper, copied, expected):
    """Defect: a foreign, pre-marker, other-lineage or ungranted-epoch replica becomes C's authority."""
    abc.release()
    root, where = abc.root, {}
    if copied:
        root = tmp_path / "copy"
        shutil.copytree(abc.root, root)
        where = dict(root=root, database=tmp_path / "copy-coordinator.sqlite", vault_id="copy")
    tamper(root)
    status, live, file_write = run_host(tmp_path, tmp_path / "state-b", "host-b", _refused_replica, **where)
    assert status["status"] == expected and not live and file_write == "committed"


# --- slices 3-6: coalesced publication, export flush, backup, divergence and reconcile ------


def _replica_meta(root):
    with closing(connection.open_reader(replica.replica_path(root))) as reader:
        return dict(reader.execute("SELECT key,value FROM store_meta"))


def _until(predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.05)


def _published(found):
    """Wait until the coalesced publisher has carried the live head into the vault replica and finished."""
    def done():
        meta = found.meta()
        head = json.loads(meta.get(schema.META_PUBLISHED_REPLICA_HEAD) or "null")
        return (schema.META_PENDING_REPLICA_PUBLICATION not in meta and head is not None
                and str(head["commit_seq"]) == meta[schema.META_COMMIT_SEQ])
    _until(done)


def _counted_publications(monkeypatch):
    """Count publications that installed a replica, orderly or coalesced."""
    calls = []

    def counted(publish, *args, **kwargs):
        result = publish(*args, **kwargs)
        if result.status == "published":
            calls.append(result)
        return result

    for name in ("publish_replica", "publish_replica_concurrently"):
        monkeypatch.setattr(replica, name, functools.partial(counted, getattr(replica, name)))
    return calls


def _stalled_copy(monkeypatch):
    """Stall every replica copy once staged, as a large store's copy and checks would, until released."""
    staged, release = threading.Event(), threading.Event()
    stage = snapshot.staged_snapshot

    @contextmanager
    def stalled(*args, **kwargs):
        with stage(*args, **kwargs) as artifact:
            staged.set()
            release.wait(60)
            yield artifact

    monkeypatch.setattr(snapshot, "staged_snapshot", stalled)
    return staged, release


def test_steady_writes_reach_the_replica_off_ack_at_most_once_per_window(abc, monkeypatch):
    """Defect: acknowledged C rows never reach the replica until release, or every write republishes."""
    from exomem.collection_store import runtime

    monkeypatch.setattr(runtime, "PUBLISH_SETTLE_SECONDS", 2.0)  # a loaded host's slow write stays in the burst
    calls = _counted_publications(monkeypatch)
    for index in range(5):
        abc.write_c(str(uuid.uuid4()), f"steady {index}")
    assert not calls  # no publication runs inside a write; the coalesced one follows the burst
    _published(abc)
    assert len(calls) == 1


def test_a_second_write_waits_out_the_window_until_the_export_flush(abc, tmp_path, monkeypatch):
    """Defect: a write inside the window republishes, or export ships a replica behind acknowledged rows."""
    from exomem import hosted_portability as portability
    from exomem.collection_store import runtime

    monkeypatch.setattr(runtime, "PUBLISH_SETTLE_SECONDS", 0.1)
    (abc.root / ".exomem/schema").mkdir(parents=True)
    (abc.root / ".exomem/schema/SKILL.md").write_text("# schema\n")
    _published(abc)
    calls = _counted_publications(monkeypatch)
    abc.write_c(LATER, "Inside the window")
    time.sleep(1)  # ten settle periods, all inside the 60 s window
    assert not calls and _replica_meta(abc.root)[schema.META_COMMIT_SEQ] != abc.meta()[schema.META_COMMIT_SEQ]
    context = dict(cell_id="cell-gate", vault_id="vault-gate", created_at="2026-10-06T00:00:00+00:00",
                   operator_authorized=True, routing_stopped=True, active_mutations=0,
                   background_writers_stopped=True, reads_allowed=True)
    exported = portability.export_quiesced_vault(abc.root, tmp_path / "exports", context=portability.PortabilityContext(
        operation_id="export-gate", lifecycle_state="quiesced", **context))
    assert len(calls) == 1
    staged = portability.prepare_restore(exported.archive_path, tmp_path / "staged", context=portability.PortabilityContext(
        operation_id="restore-gate", lifecycle_state="restore-staging", **context)).staging_root
    assert _replica_meta(staged)[schema.META_COMMIT_SEQ] == abc.meta()[schema.META_COMMIT_SEQ]
    assert not [path for path in staged.rglob("*") if path.name.endswith(("-wal", "-shm"))]


def test_a_slow_publication_copy_leaves_file_and_store_writes_acknowledging(abc, monkeypatch):
    """Defect: publication holds the vault-wide boundary while it copies, so writes refuse MUTATION_BUSY."""
    from exomem.collection_store import runtime

    monkeypatch.setattr(runtime, "PUBLISH_SETTLE_SECONDS", 0.1)
    monkeypatch.setattr(runtime, "PUBLISH_INTERVAL_SECONDS", 0.1)
    _published(abc)
    staged, release = _stalled_copy(monkeypatch)
    abc.write_c(LATER, "Starts a publication")
    assert staged.wait(10)
    try:
        started = time.monotonic()
        assert abc.write_a("A file write during the copy") == "committed"
        assert abc.write_c(str(uuid.uuid4()), "A store write during the copy")["outcome"] == "committed"
        took = time.monotonic() - started
    finally:
        release.set()
    assert took < 2.5  # the mutation timeout is 5 s; a held boundary refuses MUTATION_BUSY there
    _published(abc)  # the next window carries the writes made during the copy


def test_a_lease_lost_during_the_copy_abandons_the_swap_and_keeps_the_previous_replica(abc, monkeypatch):
    """Defect: a host that lost the lease while copying still swaps its copy into the vault."""
    from exomem.collection_store import runtime

    monkeypatch.setattr(runtime, "PUBLISH_SETTLE_SECONDS", 0.1)
    monkeypatch.setattr(runtime, "PUBLISH_INTERVAL_SECONDS", 0.1)
    _published(abc)
    previous, published = replica.replica_path(abc.root).read_bytes(), abc.meta()[schema.META_PUBLISHED_REPLICA_HEAD]
    outcomes = []
    publish = replica.publish_replica_concurrently
    monkeypatch.setattr(replica, "publish_replica_concurrently",
                        lambda *a, **k: outcomes.append(publish(*a, **k)) or outcomes[-1])
    staged, release = _stalled_copy(monkeypatch)
    assert abc.write_c(LATER, "Acknowledged before the lease moved")["outcome"] == "committed"
    assert staged.wait(10)
    abc.manager.client.release_holder("host-a", abc.manager._fencing_token)  # the coordinator hands it away
    release.set()
    _until(lambda: outcomes)
    assert (outcomes[0].status, outcomes[0].reason) == ("retry_pending", "COLLECTION_STORE_LEASE_REQUIRED")
    assert replica.replica_path(abc.root).read_bytes() == previous
    assert abc.meta()[schema.META_PUBLISHED_REPLICA_HEAD] == published


def test_export_refuses_a_lagging_replica_once_this_host_lost_the_lease(abc, tmp_path):
    """Defect: a host that can no longer flush exports a replica behind an acknowledged row."""
    from exomem import hosted_portability as portability

    (abc.root / ".exomem/schema").mkdir(parents=True)
    (abc.root / ".exomem/schema/SKILL.md").write_text("# schema\n")
    _published(abc)
    assert abc.write_c(LATER, "Acknowledged inside the window")["outcome"] == "committed"
    token = abc.manager._fencing_token
    abc.manager.client.release_holder("host-a", token)  # the coordinator hands the lease away
    refused(lambda: abc.manager._renew_collection_store(token), "WRITER_FENCED")
    context = dict(cell_id="cell-gate", vault_id="vault-gate", created_at="2026-10-06T00:00:00+00:00",
                   operator_authorized=True, routing_stopped=True, active_mutations=0,
                   background_writers_stopped=True, reads_allowed=True)
    with pytest.raises(portability.PortabilityError, match="COLLECTION_STORE_FLUSH_PENDING"):
        portability.export_quiesced_vault(abc.root, tmp_path / "exports", context=portability.PortabilityContext(
            operation_id="export-gate", lifecycle_state="quiesced", **context))


def _cli(root, *arguments):
    return subprocess.run([sys.executable, "-m", "exomem", *arguments], capture_output=True, text=True,
                          env={**os.environ, "EXOMEM_VAULT_PATH": str(root)}, timeout=120)


def test_backup_is_an_integrity_checked_snapshot_without_the_live_store(abc, tmp_path):
    """Defect: backup copies the live WAL store as a file or omits C's rows."""
    abc.release()
    result = _cli(abc.root, "collections", "backup", "--to", str(tmp_path / "backup.sqlite"))
    assert result.returncode == 0, result.stderr
    with closing(sqlite3.connect(tmp_path / "backup.sqlite")) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert conn.execute("SELECT count(*) FROM items").fetchone() == (1,)
    assert not (tmp_path / "backup.sqlite-wal").exists()


@pytest.mark.parametrize("where", ["vault", "live"])
def test_backup_refuses_the_vault_or_the_live_store(abc, tmp_path, where):
    """Defect: backup lands a store copy where vault sync carries it, or replaces the live store itself."""
    abc.release()
    target = {"vault": abc.root / "Knowledge Base/backup.sqlite", "live": abc.session.path}[where]
    before = target.read_bytes() if target.exists() else None
    result = _cli(abc.root, "collections", "backup", "--to", str(target))
    assert result.returncode == 1 and "COLLECTION_BACKUP_DESTINATION_UNSAFE" in result.stderr
    assert (target.read_bytes() if target.exists() else None) == before


def test_backup_through_a_vault_symlink_writes_only_at_its_real_target(abc, tmp_path):
    """Defect: a symlink in the vault passes the check, then the backup replaces it as a vault file."""
    abc.release()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = abc.root / "Knowledge Base/backup.sqlite"
    link.symlink_to(outside / "backup.sqlite")
    result = _cli(abc.root, "collections", "backup", "--to", str(link))
    assert result.returncode == 0, result.stderr
    assert link.is_symlink() and (outside / "backup.sqlite").is_file()
    assert not list(abc.root.rglob(".exomem-collection-backup-*"))


def test_backup_into_a_synced_root_succeeds_with_a_warning(abc, tmp_path):
    """Defect: an owner who backs up into a synced folder is refused instead of warned."""
    abc.release()
    synced = tmp_path / "synced"
    (synced / ".stfolder").mkdir(parents=True)
    result = _cli(abc.root, "collections", "backup", "--to", str(synced / "backup.sqlite"))
    assert result.returncode == 0, result.stderr
    assert "sync" in " ".join(json.loads(result.stdout)["warnings"])
    assert (synced / "backup.sqlite").exists()


def test_adopt_local_previews_the_fork_point_before_continuing(abc):
    """Defect: adopt-local continues without recording the fork point, or without an owner preview."""
    abc.release()
    result = _cli(abc.root, "collections", "adopt-local", "--why", "sync is off", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "fork_point" in json.loads(result.stdout)["preview"]


def _lease_environment(found, monkeypatch):
    """The configured lease the owner route reads, naming the gate coordinator and this host."""
    lease = {"URL": "http://localhost", "VAULT_ID": "gate", "REPLICA_ID": "host-a", "TOKEN": "lease",
             "STATE_DIR": str(found.manager.config.state_dir)}
    for name, value in lease.items():
        monkeypatch.setenv(f"EXOMEM_WRITER_LEASE_{name}", value)
    monkeypatch.setenv("EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN", "operator")


def test_hosted_tenant_cannot_preview_owner_adoption(abc):
    """RAW's hosted tenant exemption cannot grant owner maintenance authority."""
    from exomem import commands
    from exomem.governance.principal import request_scope, resolve_hosted_principal

    with request_scope(resolve_hosted_principal("tenant:unrelated")):
        refused(lambda: commands.op_maintain_memory(abc.root, mode="collections-store-adopt-local"),
                "COLLECTION_STORE_OWNER_REQUIRED")


def test_owner_route_previews_in_maintain_memory_and_applies_in_the_cli_against_the_coordinator(
        abc, monkeypatch, capsys):
    """Defect: the owner's real route cannot apply adopt-local with the configured lease and coordinator."""
    from exomem import __main__ as cli
    from exomem import commands

    abc.release()
    _lease_environment(abc, monkeypatch)
    path = replica.replica_path(abc.root)
    foreign = path.read_bytes() + b"\0"
    path.write_bytes(foreign)  # another copy published over this host's replica
    with library_scope():
        preview = commands.op_maintain_memory(abc.root, mode="collections-store-adopt-local")
    assert (preview["step"], preview["preview"]["state"]) == ("adopt-local", "foreign")
    assert cli._collections_main(["adopt-local", "--vault", str(abc.root), "--why", "the other copy is stale",
                                  "--preview-id", preview["plan_id"]]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "adopted"
    assert [evidence.read_bytes() for evidence in path.parent.glob(".foreign-*")] == [foreign]
    assert len(json.loads(abc.meta()[schema.META_FORKS])) == 1
    # The idle service continues under the adopted identity without a restart.
    assert abc.write_c(str(uuid.uuid4()), "Written after the owner adopted")["outcome"] == "committed"


def test_owner_route_applies_inside_the_running_service_which_keeps_its_lease_and_writes(abc, monkeypatch):
    """Defect: adopt-local applied while the service runs hands its lease back or strands its old identity."""
    from exomem import commands
    from exomem.collection_store import runtime

    monkeypatch.setattr(runtime, "WATCH_SECONDS", 0.05)
    _lease_environment(abc, monkeypatch)
    _published(abc)
    path = replica.replica_path(abc.root)
    path.write_bytes(path.read_bytes() + b"\0")  # another copy published over this host's replica

    def preserved():  # the marker lands before the foreign bytes move aside; preview after both
        marker = abc.meta().get(schema.META_REPLICA_DIVERGENCE)
        return marker is not None and (path.parent / json.loads(marker)["foreign_leaf"]).exists()

    _until(preserved)
    token = abc.manager._fencing_token
    with library_scope():
        preview = commands.op_maintain_memory(abc.root, mode="collections-store-adopt-local")
        applied = commands.op_maintain_memory(abc.root, mode="collections-store-adopt-local", apply=True,
                                              plan_id=preview["plan_id"], why="the other copy is stale")
    assert (preview["step"], applied["status"]) == ("adopt-local", "adopted"), applied.get("reason")
    assert abc.manager._fencing_token == token
    assert abc.write_c(LATER, "Service write after adopt-local")["outcome"] == "committed"
    assert abc.manager.status()["collection_store"] == {"status": "admitted"}


def _adopt(found, why):
    preview = admission.adopt_local(found.session, found.manager, why=why, fence_client=found.operator)
    refused(lambda: admission.adopt_local(found.session, found.manager, why=why, preview_id="0" * 64,
                                          fence_client=found.operator), "COLLECTION_STORE_ADOPT_PREVIEW_STALE")
    adopted = admission.adopt_local(found.session, found.manager, why=why, preview_id=preview["preview_id"],
                                    fence_client=found.operator)
    return preview["preview"], adopted


def test_publication_diverges_on_a_same_signature_rewrite_and_adopt_local_keeps_the_foreign_bytes(
        abc, monkeypatch):
    """Defect: a foreign rewrite keeping inode, size and mtime is published over, or adopt-local drops its bytes."""
    from exomem.collection_store import runtime

    monkeypatch.setattr(runtime, "PUBLISH_INTERVAL_SECONDS", 0.1)  # the next write publishes promptly
    _published(abc)
    path = replica.replica_path(abc.root)
    signature, stamp = takeover.replica_signature(abc.root), path.stat()
    rewritten = bytearray(path.read_bytes())
    rewritten[-1] ^= 0xFF
    with open(path, "r+b") as file:
        file.write(rewritten)
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert takeover.replica_signature(abc.root) == signature
    assert abc.write_c(KEY, "Before the next publication")["outcome"] == "committed"
    _until(lambda: abc.manager.status()["collection_store"]["status"] == "diverged")
    refused(lambda: abc.write_c(LATER, "Refused"), "COLLECTION_STORE_DIVERGED")
    assert abc.read_c() == ["Before the next publication", "Canonical"]
    assert abc.write_a("A beside divergence") == "committed"
    instance = abc.meta()[schema.META_INSTANCE_ID]
    preview, adopted = _adopt(abc, "the rewrite was a stray editor")
    evidence = [entry for entry in preview["fork_point"]["foreign"] if entry["source"] == "evidence"]
    assert [entry["sha256"] for entry in evidence] == [hashlib.sha256(rewritten).hexdigest()]
    assert adopted["status"] == "adopted", adopted.get("reason")
    assert abc.write_c(LATER, "After adopt")["outcome"] == "committed"
    assert (path.parent / evidence[0]["leaf"]).read_bytes() == rewritten
    meta = abc.meta()
    forks = json.loads(meta[schema.META_FORKS])
    assert [fork["local"]["commit_seq"] for fork in forks] == [3]
    assert forks[0]["divergence"]["source_leaf"] == "Knowledge Base/_Collections/collections.sqlite"
    assert json.loads(meta[schema.META_LINEAGE])[-1]["adopted_from"] == instance
    _published(abc)
    assert _replica_meta(abc.root)[schema.META_INSTANCE_ID] == meta[schema.META_INSTANCE_ID]


def test_adopt_local_abandons_the_publication_a_divergence_interrupted(abc, monkeypatch):
    """Defect: an interrupted publication's intent re-diverges every adopt-local, or its workspace stays forever."""
    from exomem.collection_store import runtime

    monkeypatch.setattr(runtime, "PUBLISH_INTERVAL_SECONDS", 0.1)  # the next write publishes promptly
    _published(abc)
    path = replica.replica_path(abc.root)
    resolve = replica._Publisher.resolve

    def foreign_write_lands(publisher, pending, **kwargs):
        if pending["phase"] == "ready":
            monkeypatch.setattr(replica._Publisher, "resolve", resolve)
            path.write_bytes(path.read_bytes() + b"\0")
        return resolve(publisher, pending, **kwargs)

    monkeypatch.setattr(replica._Publisher, "resolve", foreign_write_lands)
    assert abc.write_c(LATER, "Mid publication")["outcome"] == "committed"
    _until(lambda: abc.manager.status()["collection_store"]["status"] == "diverged")
    assert schema.META_PENDING_REPLICA_PUBLICATION in abc.meta()
    assert list(path.parent.glob(".exomem-collection-replica-work-*"))
    preview, adopted = _adopt(abc, "the write mid-publication was a stray editor")
    assert adopted["status"] == "adopted", adopted.get("reason")
    assert preview["abandoned_publication"] is not None and adopted["abandoned_workspace"] == "removed"
    assert not list(path.parent.glob(".exomem-collection-replica-work-*"))
    assert abc.write_c(str(uuid.uuid4()), "After adopt")["outcome"] == "committed"


def test_a_view_stamp_divergence_reports_diverged_until_adopt_local_clears_it(abc):
    """Defect: a view stamped by another store fences writes with no owner exit while status says admitted."""
    with closing(sqlite3.connect(abc.session.path)) as conn:  # what edit-back records for a foreign view stamp
        conn.execute("INSERT INTO store_meta(key,value) VALUES (?,'1')", (schema.META_VIEW_DIVERGED,))
        conn.commit()
    assert abc.manager.status()["collection_store"]["status"] == "diverged"
    refused(lambda: abc.write_c(LATER, "Refused"), "COLLECTION_STORE_DIVERGED")
    preview, adopted = _adopt(abc, "the foreign view was a stale copy")
    assert (preview["state"], preview["view_diverged"], adopted["status"]) == ("diverged", True, "adopted")
    assert abc.write_c(LATER, "After adopt")["outcome"] == "committed"
    assert abc.manager.status()["collection_store"] == {"status": "admitted"}


def _adopt_local_from_sync_pending(found):
    pending = found.open()
    preview = admission.adopt_local(found.session, found.manager, why="sync is off", fence_client=found.operator)
    applied = admission.adopt_local(found.session, found.manager, why="sync is off",
                                    preview_id=preview["preview_id"], fence_client=found.operator)
    outcome = found.write_c(LATER, "After adopt-local on B")["outcome"]
    found.release()
    return (pending["status"], preview["preview"]["replica"]["published_by_this_store"], applied["status"],
            applied.get("reason"), outcome, found.meta()[schema.META_INSTANCE_ID])


def test_adopt_local_from_sync_pending_continues_past_this_hosts_own_replica(abc, tmp_path):
    """Defect: adopt-local, the remedy SYNC_PENDING names, leaves the host pending and its store diverged."""
    abc.release()
    run_host(tmp_path, tmp_path / "state-b", "host-b", _adopt_and_release)
    own = replica.replica_path(abc.root).read_bytes()  # B's last publication
    assert abc.write_c(KEY, "Newer on A")["outcome"] == "committed"
    abc.release()
    replica.replica_path(abc.root).write_bytes(own)  # A's newer replica has not reached B
    pending, ours, status, reason, outcome, instance = run_host(
        tmp_path, tmp_path / "state-b", "host-b", _adopt_local_from_sync_pending)
    assert (pending, ours, status, outcome) == ("sync_pending", True, "adopted", "committed"), reason
    assert abc.manager.client.status().collection_store_head.instance_id == instance
    assert _replica_meta(abc.root)[schema.META_INSTANCE_ID] == instance


def _write_on_the_copy(found):
    assert found.open() == {"status": "admitted"}
    found.write_c(LATER, "From the other host")
    found.write_c(KEY, "Written on both hosts")
    found.release()


def test_reconcile_holds_every_foreign_item_change_after_the_fork_point(abc, tmp_path):
    """Defect: reconcile loses the other side's writes, edits rows silently, or its holds miss the replica."""
    abc.release()  # the other host starts from A's flushed replica
    copy = tmp_path / "copy"
    shutil.copytree(abc.root, copy)
    run_host(tmp_path, tmp_path / "state-b", "host-b", _write_on_the_copy, root=copy,
             database=tmp_path / "copy-coordinator.sqlite", vault_id="copy")
    assert abc.write_c(KEY, "Written on both hosts")["outcome"] == "committed"  # the same row as the copy's
    assert abc.write_c(str(uuid.uuid4()), "Kept on A")["outcome"] == "committed"
    _published(abc)
    shutil.copyfile(replica.replica_path(copy), abc.root / "delivery.tmp")
    os.replace(abc.root / "delivery.tmp", replica.replica_path(abc.root))  # sync delivers it
    _until(lambda: abc.manager.status()["collection_store"]["status"] == "diverged")
    _adopt(abc, "keep this host, hold the other")
    plan = admission.reconcile_store(abc.session, abc.manager, why="hold the other host's writes",
                                     fence_client=abc.operator)
    assert [(item["collection_id"], item["item_key"]) for item in plan["preview"]["items"]] == [(CID, LATER)]
    assert plan["preview"]["unchanged"] == 1
    result = admission.reconcile_store(abc.session, abc.manager, why="hold the other host's writes",
                                       preview_id=plan["preview_id"], fence_client=abc.operator)
    assert result["status"] == "held"
    assert abc.read_c() == ["Canonical", "Kept on A", "Written on both hosts"]
    with closing(connection.open_reader(abc.session.path)) as reader:
        held = reader.execute("SELECT kind,code,held_bytes,diagnostics_json FROM held_candidates").fetchall()
    assert [(kind, code) for kind, code, _, _ in held] == [("view-correction", "COLLECTION_STORE_DIVERGED")]
    assert b"From the other host" in held[0][2] and json.loads(held[0][3])[0]["common_ancestor_commit_seq"] == 2
    again = admission.reconcile_store(abc.session, abc.manager, why="again", fence_client=abc.operator)
    assert again["preview"]["items"] == []
    abc.release()  # an orderly release carries the reconciliation into the vault replica
    with closing(connection.open_reader(replica.replica_path(abc.root))) as reader:
        assert reader.execute("SELECT code FROM held_candidates").fetchall() == [("COLLECTION_STORE_DIVERGED",)]
        marker = reader.execute("SELECT value FROM store_meta WHERE key=?", (schema.META_RECONCILED_FOREIGN,)).fetchone()
    assert json.loads(marker[0]) == [source["sha256"] for source in plan["preview"]["sources"]]


def _write_and_create_on_the_copy(found):
    assert found.open() == {"status": "admitted"}
    found.write_c(LATER, "From the other host")
    created = admission.create_new(found.session, found.manager, manifest_path().replace("Work", "Copy"),
                                   summary_text().replace(CID, ONLY_ON_COPY), why="a collection only the copy has",
                                   request_id="create-d", fence_client=found.operator)
    assert created["status"] == "marker_admitted", created
    with preview_store(found.root, found.manager._collection_store) as writer:
        with found.manager.mutation_guard(found.root):
            writer.append_record(ONLY_ON_COPY, item={"title": "Only on the copy"}, item_key=ROW, why="gate write")
    found.release()


def _change_later_on_the_copy(found):
    assert found.open() == {"status": "admitted"}
    with preview_store(found.root, found.manager._collection_store) as writer:
        with found.manager.mutation_guard(found.root):
            with writer._authorization():
                container = writer._container(writer._collection(CID)[0])
            row_version, payload_hash = writer.connection.execute(
                "SELECT row_version,payload_hash FROM items WHERE collection_id=? AND item_key=?", (CID, LATER)).fetchone()
            version = tokens.item_version(CID, LATER, row_version, payload_hash)
            writer.update_record(CID, item_key=LATER, changes={"title": "Changed again on the other host"},
                                 expected_container_hash=container, expected_item_version=version, why="gate write")
    found.release()


def _held_later(found):
    """The foreign row version of each LATER hold: one hold, at the newest change delivered."""
    with closing(connection.open_reader(found.session.path)) as reader:
        rows = reader.execute("SELECT candidate_json,diagnostics_json FROM held_candidates").fetchall()
    return [json.loads(diagnostics)[0]["foreign_row_version"]
            for candidate, diagnostics in rows if json.loads(candidate)["item_key"] == LATER]


def _deliver_and_adopt(found, copy, why):
    """A writes, the copy's replica arrives over A's, and the owner keeps A."""
    assert found.write_c(str(uuid.uuid4()), f"On A before: {why}")["outcome"] == "committed"
    _published(found)
    shutil.copyfile(replica.replica_path(copy), found.root / "delivery.tmp")
    os.replace(found.root / "delivery.tmp", replica.replica_path(found.root))
    _until(lambda: found.manager.status()["collection_store"]["status"] == "diverged")
    assert _adopt(found, why)[1]["status"] == "adopted"


def test_reconcile_keeps_evidence_it_cannot_fully_hold_and_holds_each_item_once(abc, tmp_path, monkeypatch):
    """Defect: reconcile marks evidence done while dropping part of its delta, holds one item twice,
    re-records a repeat, or leaves the owner no exit from evidence it cannot hold while the service
    holds the lease."""
    from exomem import commands
    from exomem.collection_store import runtime

    monkeypatch.setattr(runtime, "PUBLISH_INTERVAL_SECONDS", 0.1)  # each round's write publishes promptly
    abc.release()
    copy = tmp_path / "copy"
    shutil.copytree(abc.root, copy)
    elsewhere = dict(root=copy, database=tmp_path / "copy-coordinator.sqlite", vault_id="copy")
    run_host(tmp_path, tmp_path / "state-b", "host-b", _write_and_create_on_the_copy, **elsewhere)
    _deliver_and_adopt(abc, copy, "keep A over the first delivery")
    plan = admission.reconcile_store(abc.session, abc.manager, why="hold", fence_client=abc.operator)
    assert [(item["collection_id"], item["item_key"]) for item in plan["preview"]["items"]] == [(CID, LATER)]
    assert [(entry["collection_id"], entry["reason"]) for entry in plan["preview"]["skipped"]] == [
        (ONLY_ON_COPY, "collection absent from this store")]
    assert plan["preview"]["reconciled"] == []
    first = admission.reconcile_store(abc.session, abc.manager, why="hold", preview_id=plan["preview_id"],
                                      fence_client=abc.operator)
    assert _held_later(abc) == [1]
    run_host(tmp_path, tmp_path / "state-b", "host-b", _change_later_on_the_copy, **elsewhere)
    _deliver_and_adopt(abc, copy, "keep A over the second delivery")
    plan = admission.reconcile_store(abc.session, abc.manager, why="hold again", fence_client=abc.operator)
    assert len(plan["preview"]["sources"]) == 2 and plan["preview"]["reconciled"] == []
    second = admission.reconcile_store(abc.session, abc.manager, why="hold again", preview_id=plan["preview_id"],
                                       fence_client=abc.operator)
    assert (second["held_ids"], second["superseded"]) == (first["held_ids"], 1)
    assert _held_later(abc) == [2]
    sequence = abc.meta()[schema.META_COMMIT_SEQ]
    for _ in range(2):  # both files stay unreconciled; neither the older change nor a repeat moves the hold
        plan = admission.reconcile_store(abc.session, abc.manager, why="nothing new", fence_client=abc.operator)
        repeat = admission.reconcile_store(abc.session, abc.manager, why="nothing new",
                                           preview_id=plan["preview_id"], fence_client=abc.operator)
        assert (plan["preview"]["items"], repeat["held_ids"], repeat["transitions"]) == ([], [], [])
        assert (abc.meta()[schema.META_COMMIT_SEQ], _held_later(abc)) == (sequence, [2])
    _lease_environment(abc, monkeypatch)
    # The owner's exit while the service holds the lease: maintain_memory, applied in the serving session.
    acknowledged = commands.op_maintain_memory(
        abc.root, mode="collections-store-adopt-local", apply=True, plan_id=plan["preview_id"],
        why="the copy-only collection is gone", acknowledge_skipped=True)
    assert {tuple(receipt["ids"]["skipped_collection_absent"]) for receipt in acknowledged["transitions"]} == {
        tuple(f"{entry['collection_id']}:{entry['item_key']}" for entry in plan["preview"]["skipped"])}
    assert sorted(acknowledged["reconciled"]) == sorted(source["sha256"] for source in plan["preview"]["sources"])
    assert admission.reconcile_store(abc.session, abc.manager, why="done", fence_client=abc.operator)[
        "preview"]["sources"] == []


# --- later slices: strict xfails naming the slice that turns them green -----------------


def test_supported_runtime_admits_a_fresh_root_copy_of_the_c_vault(abc, tmp_path, monkeypatch):
    """Defect: the supported release itself can never start on a C vault copy or restore."""
    copy = tmp_path / "copy"
    shutil.copytree(abc.root, copy)
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "fresh-state"))
    assert "collections-store-v1" in state_migration.supported_state_compatibility_ids()
    state_migration.require_vault_state_ready(copy)


def test_staged_restore_carries_the_replica_and_admits_the_c_vault(abc, tmp_path):
    """Defect: a restore drops C's replica, carries live WAL bytes, or refuses the supported runtime."""
    from conftest import _drain_background_threads
    from test_hosted_restore_candidate import _bootstrap, _request

    from exomem import hosted_portability, hosted_restore
    from exomem.init import init_vault

    abc.release()
    init_vault(abc.root, force=True)
    _drain_background_threads()  # Source workers must finish before restore changes the process state root.
    context = hosted_portability.PortabilityContext(
        cell_id="source-cell", vault_id="logical-vault", operation_id="export-gate",
        created_at="2026-10-06T00:00:00+00:00", operator_authorized=True, lifecycle_state="quiesced",
        routing_stopped=True, active_mutations=0, background_writers_stopped=True, reads_allowed=True)
    exported = hosted_portability.export_quiesced_vault(abc.root, tmp_path / "exports", context=context)
    restored = hosted_restore.restore_candidate(_request(tmp_path, exported), bootstrap_security=_bootstrap)
    assert restored.status == "ready"
    target = tmp_path / "target-vault"
    assert authority.read_marker(target) == authority.read_marker(abc.root)
    assert ab_bytes(target) == ab_bytes(abc.root)
    assert not [path for path in target.rglob("*") if path.name.endswith(("-wal", "-shm"))]
    assert run_host(tmp_path, tmp_path / "restored-state", "restore", _adopt_fresh_copy,
                    root=target, database=tmp_path / "restore-coordinator.sqlite") == "committed"


def test_slice_rollback_disables_c_but_keeps_its_data_and_a_b(abc):
    """Defect: rolling back the slice deletes C's routing/rows/replica or disables A/B."""
    marker = authority.read_marker(abc.root)
    admission.rollback_slice(abc.session, abc.manager)
    refused(abc.read_c, "COLLECTION_STORE_DISABLED")
    refused(lambda: admission.resume_local(abc.session, abc.manager, fence_client=abc.operator),
            "COLLECTION_STORE_DISABLED")
    assert authority.read_marker(abc.root) == marker
    assert abc.meta()[schema.META_COMMIT_SEQ] == "2"
    assert abc.write_a("A after rollback") == "committed"


def test_rollback_reports_disable_when_publication_fails(abc, monkeypatch):
    """An unavailable replica cannot hide that the local producer has already stopped."""
    def unavailable(*args):
        raise OSError("replica unavailable")

    monkeypatch.setattr(admission, "_publish_current_epoch", unavailable)
    result = admission.rollback_slice(abc.session, abc.manager)
    assert result["status"] == "disabled" and result["publication"] == "pending"
    assert capability.records_summary_disabled(abc.root)
    assert abc.write_a("A after failed publication") == "committed"


def test_marker_profile_must_match_the_canonical_collection(abc):
    """A marker edit cannot make a Records store disappear from a profile-filtered inventory."""
    marker = json.loads(authority.read_marker(abc.root))
    marker["collections"][0]["semantic_profile"] = "planning"
    authority.marker_path(abc.root).write_text(json.dumps(marker))
    refused(abc.read_c, "COLLECTION_STORE_MARKER_CONFLICT")


def test_failed_close_stops_renewing_an_unadmitted_lease(ab):
    """A process surviving failed shutdown must not keep the next writer fenced forever."""
    ab.session.bind(ab.manager)
    ab.session.runtime()
    ab.manager.ensure_writer()
    ab.manager.start_renewer()
    refused(ab.manager.close, "COLLECTION_STORE_FLUSH_PENDING")
    ab.manager._renewer.join(2)
    assert not ab.manager._renewer.is_alive()


def _adopt_fresh_copy(found):
    with pytest.MonkeyPatch.context() as patch:
        _lease_environment(found, patch)
        preview = admission.adopt_local_route(found.root)
        assert not found.session.path.exists()
        refused(lambda: admission.adopt_local_route(found.root, why="copy", preview_id="0" * 64),
                "COLLECTION_STORE_ADOPT_PREVIEW_STALE")
        assert not found.session.path.exists()
        result = admission.adopt_local_route(found.root, why="copy", preview_id=preview["preview_id"])
    assert result["status"] == "admitted"
    assert found.open()["status"] == "admitted"
    assert found.read_c() == ["Canonical"]
    assert found.write_a("A after copy") == "committed"
    return found.write_c(LATER, "C after copy")["outcome"]


def test_fresh_copy_adoption_previews_before_installing_the_replica(abc, tmp_path):
    """A copied vault must preview and adopt without an already existing local store."""
    abc.release()
    copy = tmp_path / "copy"
    shutil.copytree(abc.root, copy)
    assert run_host(tmp_path, tmp_path / "copy-state", "copy", _adopt_fresh_copy,
                    root=copy, database=tmp_path / "copy-coordinator.sqlite") == "committed"


def test_launcher_refuses_an_actual_v1_marker_candidate_before_handoff(abc, tmp_path, monkeypatch):
    """A store-aware candidate that passes capability admission but cannot preserve version-two path ownership."""
    import asyncio

    from exomem.service_manager import WorkerRuntime

    interpreter = os.environ.get("EXOMEM_TEST_MARKER_V1_PYTHON")
    if not interpreter:
        pytest.skip("requires an installed version-one marker candidate")
    before = authority.read_marker(abc.root)
    assert authority.parse_marker(abc.root, before)["version"] == 2
    copy = tmp_path / "marker-copy"
    shutil.copytree(abc.root, copy)
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "marker-fresh-state"))
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(copy))
    probe = subprocess.run([interpreter, "-I", "-c", "import json; from importlib.metadata import version; "
        "from exomem.state_migration import supported_state_compatibility_ids; "
        "print(json.dumps([version('exomem'), supported_state_compatibility_ids()]))"],
        capture_output=True, text=True, check=True)
    version, supported = json.loads(probe.stdout)
    assert "collections-store-v1" in supported and state_migration.COLLECTION_MARKER_COMPATIBILITY_ID not in supported
    runtime = WorkerRuntime(tmp_path / "marker-worker.sock", host="127.0.0.1", port=8765)
    target = asyncio.run(runtime.inspect({"python": interpreter, "version": version}))
    with pytest.raises(ValueError, match="does not support required state compatibility"):
        runtime.migration_required(target)
    assert authority.read_marker(copy) == before


@pytest.mark.parametrize("field,value", [("source_path", "Knowledge Base/Records/Other/Items"),
                                          ("layout", "markdown-log")])
def test_marker_ownership_must_match_canonical_storage(abc, field, value):
    """A valid-shaped marker that contradicts the canonical source declaration."""
    marker = json.loads(authority.read_marker(abc.root))
    marker["collections"][0][field] = value
    authority.marker_path(abc.root).write_text(json.dumps(marker))
    refused(abc.read_c, "COLLECTION_STORE_MARKER_CONFLICT")


def test_log_marker_ownership_preserves_structural_path_boundaries(abc):
    """A log namespace that either misses portable aliases or claims unrelated siblings and malformed fragments."""
    marker = json.loads(authority.read_marker(abc.root))
    entry = marker["collections"][0]
    entry.update(layout="markdown-log", source_path="Knowledge Base/Records/Detached/Log.md")
    marker = authority.parse_marker(abc.root, json.dumps(marker))
    for path in (entry["source_path"], entry["source_path"] + "#" + KEY,
                 "Knowledge Base/Records/Detached/./LOG.md#" + KEY,
                 "Knowledge Base/Records/Work/Held/unknown.md",
                 "Knowledge Base/Records/Work/_history/unknown.md"):
        assert authority.owned_entry(abc.root, marker, path)["collection_id"] == CID
    for path in (entry["source_path"] + "/other.md", entry["source_path"] + "#not-an-item",
                 "Knowledge Base/Records/Detached/Other.md", "Knowledge Base/Records/Work/Notes.md"):
        assert authority.owned_entry(abc.root, marker, path) is None
