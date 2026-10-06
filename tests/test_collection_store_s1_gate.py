"""S1.2a gate: file Records A, file Planning B and a NEW store collection C in one vault.

Every case drives the real writer lease against a real coordinator database and
the production producer session on a real vault path. Restarts and other hosts
are separate processes with their own state roots; older-reader cases run the
actual older interpreter. Cases owned by a later slice are strict xfails naming
it. No case may let C fall back to files, migrate A/B or expose a half-created C.
"""

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
from test_collection_store_writer import CID, KEY, OTHER, manifest_path, manifest_text

from exomem import records, state_migration
from exomem.cli_ops import OpError
from exomem.collection_store import (
    admission,
    authority,
    connection,
    custody,
    replica,
    schema,
    takeover,
)
from exomem.collection_store.connection import CollectionStoreError
from exomem.collection_store.preview import preview_store
from exomem.plan_memory import plan_memory
from exomem.record_memory import record_memory

A_PATH = manifest_path("records").replace("Work", "Legacy")
B_PATH = manifest_path("planning").replace("Work", "Legacy")
ROW = "33333333-3333-4333-8333-333333333333"
LATER = "44444444-4444-4444-8444-444444444444"
OLDER = os.environ.get("EXOMEM_TEST_OLDER_READER_PYTHON", "")
# Other hosts are forked processes; the context is created only inside run_host, so the
# module still imports (and its fork-free cases still run) where fork does not exist.
requires_fork = pytest.mark.skipif("fork" not in multiprocessing.get_all_start_methods(),
                                   reason="requires fork")


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


def run_host(base, state, name, action, *, root=None, database=None, vault_id="gate", exit_code=0):
    """Run one host in a fresh process: its own state root, lease state and session."""
    fork = multiprocessing.get_context("fork")
    receiver, sender = fork.Pipe(duplex=False)

    def main():
        os.environ["EXOMEM_STATE_ROOT"] = str(state)
        try:
            with coordinator(database or base / "coordinator.sqlite"), \
                    host(root or base / "vault", base, name, vault_id=vault_id) as found:
                sender.send(("ok", action(found)))
        except BaseException as error:  # noqa: BLE001 - reported to the parent assertion
            sender.send(("error", f"{type(error).__name__}: {error}"))
            os._exit(1)
        os._exit(0)

    child = fork.Process(target=main)
    child.start()
    sender.close()
    child.join(90)
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
    return admission.create_new(found.session, found.manager, manifest_path(), manifest_text(), **arguments)


def refused(call, code):
    with pytest.raises((CollectionStoreError, OpError)) as error:
        call()
    assert code in str(error.value)
    return str(error.value)


@pytest.fixture
def ab(tmp_path, monkeypatch):
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("EXOMEM_COLLECTION_STORE_PREVIEW", "1")
    monkeypatch.delenv(custody.SYNC_ROOTS_ENV, raising=False)
    monkeypatch.setattr("exomem.records._capture_sweep_carrier", lambda *a, **kw: None)
    monkeypatch.setattr("exomem.records._due_state_carrier", lambda *a, **kw: None)
    root = tmp_path / "vault"
    (root / "Knowledge Base").mkdir(parents=True)
    (root / "Knowledge Base/log.md").write_text("# Activity\n")
    with coordinator(tmp_path / "coordinator.sqlite"), host(root, tmp_path, "host-a") as found:
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


def test_gate_imports_where_fork_does_not_exist(monkeypatch):
    """Defect: importing the gate creates a fork context, so a fork-less platform cannot collect it."""
    import importlib.util

    def no_fork(method=None):
        raise ValueError(f"cannot find context for {method!r}")

    monkeypatch.setattr(multiprocessing, "get_context", no_fork)
    monkeypatch.setattr(multiprocessing, "get_all_start_methods", lambda: ["spawn"])
    spec = importlib.util.spec_from_file_location("gate_without_fork", __file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.requires_fork.args == (True,)


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


@requires_fork
@pytest.mark.parametrize("cut", ["before", "after"])
def test_crash_around_marker_cutover_recovers_the_same_create_once(ab, cut, tmp_path):
    """Defect: a restarted process re-executes C's create, loses its receipt or leaves C half-routed."""
    before = ab_bytes(ab.root)

    def crash(found):
        rename = found.session.fs.rename

        def cut_marker(source, parent, leaf, **kwargs):
            if leaf != "mode.json":
                return rename(source, parent, leaf, **kwargs)
            if cut == "after":
                rename(source, parent, leaf, **kwargs)
            os._exit(73)

        found.session.fs.rename = cut_marker
        create_c(found)

    run_host(tmp_path, tmp_path / "state", "host-a", crash, exit_code=73)
    with closing(connection.open_reader(ab.session.path)) as reader:
        intent = authority.pending_create(reader)
        assert reader.execute("SELECT count(*) FROM txns").fetchone() == (1,)
    assert intent is not None
    marker = authority.read_marker(ab.root)
    assert marker == (None if cut == "before" else intent["target_marker"].encode())
    assert not (ab.root / manifest_path()).exists()

    result = run_host(tmp_path, tmp_path / "state", "host-a",
                      lambda found: admission.resume_local(found.session, found.manager,
                                                           fence_client=found.operator))
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
    assert started.returncode != 0 and "StateCompatibilityUnsupported" in started.stderr
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


@requires_fork
def test_restart_reacquires_its_own_head_without_waiting(abc, tmp_path):
    """Defect: a restarted host waits on, re-adopts or refuses its own recorded head."""
    instance = abc.meta()[schema.META_INSTANCE_ID]
    abc.release()

    def restart(found):
        return found.open(), found.write_c(LATER, "After restart")["outcome"], found.meta()

    status, outcome, meta = run_host(tmp_path, tmp_path / "state", "host-a", restart)
    assert status == {"status": "admitted"} and outcome == "committed"
    assert meta[schema.META_INSTANCE_ID] == instance


@requires_fork
def test_lease_handoff_adopts_the_newer_replica_in_both_directions(abc, tmp_path):
    """Defect: a new holder writes on its stale local store instead of adopting the recorded head."""
    first = abc.meta()[schema.META_INSTANCE_ID]
    before = ab_bytes(abc.root)
    abc.release()

    def host_b(found):
        status = found.open()
        rows = found.read_c()
        found.write_c(LATER, "From B")
        meta = found.meta()
        found.release()
        return status, rows, meta

    status, rows, meta = run_host(tmp_path, tmp_path / "state-b", "host-b", host_b)
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


@requires_fork
def test_lagging_replica_is_sync_pending_with_reads_until_delivery(abc, tmp_path):
    """Defect: a holder behind the recorded head writes over it, blocks reads, A/B or the lease, or never rechecks."""
    abc.release()

    def adopt_and_release(found):
        assert found.open() == {"status": "admitted"}
        found.release()

    run_host(tmp_path, tmp_path / "state-b", "host-b", adopt_and_release)
    lagging = replica.replica_path(abc.root).read_bytes()
    assert abc.write_c(KEY, "Newer on A")["outcome"] == "committed"
    # B handed back the head A already had: A records its tenure in place, no re-adoption.
    assert not list(abc.session.path.parent.glob("*" + takeover.SUPERSEDED + "*"))
    abc.release()
    delivered = replica.replica_path(abc.root).read_bytes()
    replica.replica_path(abc.root).write_bytes(lagging)

    def behind(found):
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

    pending, rows, refusal, file_write = run_host(tmp_path, tmp_path / "state-b", "host-b", behind)
    assert pending["status"] == "sync_pending"
    assert (pending["recorded_commit_seq"], pending["local_commit_seq"]) == (3, 2)
    assert rows == ["Canonical"]
    assert "adopt-local" in refusal and file_write == "committed"
    assert abc.write_a("A while B waits") == "committed"
    abc.release()

    def delivery(found):
        takeover.ATTENTION_SECONDS = 0
        found.open()
        attention = found.manager.status()["collection_store"]["attention"]
        replica.replica_path(found.root).write_bytes(delivered)
        return attention, found.write_c(LATER, "After delivery")["outcome"], found.read_c()

    attention, outcome, final = run_host(tmp_path, tmp_path / "state-b", "host-b", delivery)
    assert attention is True
    assert outcome == "committed" and final == ["After delivery", "Canonical", "Newer on A"]


@requires_fork
def test_fork_is_durably_diverged_with_reads_and_a_b_writes_continuing(abc, tmp_path):
    """Defect: a host whose unpublished commits fork from the replica adopts over them or keeps writing."""
    abc.release()

    def write_and_crash(found):
        assert found.open() == {"status": "admitted"}
        found.write_c(LATER, "Unpublished on B")
        os._exit(73)

    run_host(tmp_path, tmp_path / "state-b", "host-b", write_and_crash, exit_code=73)
    stranded = abc.manager.client.status()
    abc.manager.client.release_holder("host-b", stranded.fencing_token)
    assert abc.write_c(KEY, "A after fork")["outcome"] == "committed"
    abc.release()

    def restart_b(found):
        status = found.open()
        rows = found.read_c()
        refused(lambda: found.write_c(OTHER, "Refused"), "COLLECTION_STORE_DIVERGED")
        with preview_store(found.root, found.manager._collection_store):
            file_write = found.write_a("A beside divergence")
        found.manager.close()
        return status, rows, file_write, found.meta()

    status, rows, file_write, meta = run_host(tmp_path, tmp_path / "state-b", "host-b", restart_b)
    assert status["status"] == "diverged" and rows == ["Canonical", "Unpublished on B"]
    assert file_write == "committed" and schema.META_REPLICA_DIVERGENCE in meta
    # The diverged host hands the lease back without replacing the recorded head.
    recorded = abc.manager.client.status().collection_store_head
    assert recorded.instance_id == abc.meta()[schema.META_INSTANCE_ID]
    assert abc.read_c() == ["A after fork", "Canonical"]


@requires_fork
def test_copied_vault_on_fresh_state_and_coordinator_adopts_the_replica(abc, tmp_path):
    """Defect: a copied vault falls back C to files, imports A/B, cannot write C, or strands a late replica."""
    abc.release()
    copy = tmp_path / "copy"
    shutil.copytree(abc.root, copy)
    original, copied_ab = file_bytes(abc.root), ab_bytes(copy)
    delivered = replica.replica_path(copy).read_bytes()
    _truncated(copy)

    def adopt(found):
        pending = found.open()
        replica.replica_path(found.root).write_bytes(delivered)
        return pending, found.open(), found.read_c(), found.write_c(LATER, "On the copy")["outcome"]

    pending, status, rows, outcome = run_host(
        tmp_path, tmp_path / "state-copy", "copy-host", adopt, root=copy,
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
    marker["collections"].append({"collection_id": LATER, "authority": "store", "store_id": marker["store_id"],
                                  "manifest_path": manifest_path().replace("Work", "Later")})
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


@requires_fork
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

    def adopt(found):
        status = found.open()
        refused(lambda: record_memory(found.root, "query", collection=CID), "")
        return status, found.session.path.exists(), found.write_a("A beside the refused replica")

    status, live, file_write = run_host(tmp_path, tmp_path / "state-b", "host-b", adopt, **where)
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
    calls = []
    publish = replica.publish_replica
    monkeypatch.setattr(replica, "publish_replica", lambda *a, **k: calls.append(1) or publish(*a, **k))
    return calls


def test_steady_writes_reach_the_replica_off_ack_at_most_once_per_window(abc, monkeypatch):
    """Defect: acknowledged C rows never reach the replica until release, or every write republishes."""
    from exomem.collection_store import runtime

    monkeypatch.setattr(runtime, "PUBLISH_SETTLE_SECONDS", 2.0)  # a loaded host's slow write stays in the burst
    calls = _counted_publications(monkeypatch)
    for index in range(5):
        abc.write_c(str(uuid.uuid4()), f"steady {index}")
    assert not calls  # publication stays off the acknowledgement path
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


@pytest.mark.parametrize("where", ["vault", "synced", "live"])
def test_backup_refuses_the_vault_a_synced_root_or_the_live_store(abc, tmp_path, where):
    """Defect: backup lands a store copy where sync carries it, or replaces the live store itself."""
    abc.release()
    synced = tmp_path / "synced"
    (synced / ".stfolder").mkdir(parents=True)
    target = {"vault": abc.root / "Knowledge Base/backup.sqlite", "synced": synced / "backup.sqlite",
              "live": abc.session.path}[where]
    before = target.read_bytes() if target.exists() else None
    result = _cli(abc.root, "collections", "backup", "--to", str(target))
    assert result.returncode == 1 and "COLLECTION_BACKUP_DESTINATION_UNSAFE" in result.stderr
    assert (target.read_bytes() if target.exists() else None) == before


def test_adopt_local_previews_the_fork_point_before_continuing(abc):
    """Defect: adopt-local continues without recording the fork point, or without an owner preview."""
    abc.release()
    result = _cli(abc.root, "collections", "adopt-local", "--why", "sync is off", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "fork_point" in json.loads(result.stdout)["preview"]


def _adopt(found, why):
    preview = admission.adopt_local(found.session, found.manager, why=why, fence_client=found.operator)
    refused(lambda: admission.adopt_local(found.session, found.manager, why=why, preview_id="0" * 64,
                                          fence_client=found.operator), "COLLECTION_STORE_ADOPT_PREVIEW_STALE")
    adopted = admission.adopt_local(found.session, found.manager, why=why, preview_id=preview["preview_id"],
                                    fence_client=found.operator)
    return preview["preview"], adopted


def test_watch_diverges_on_a_same_signature_rewrite_and_adopt_local_keeps_the_foreign_bytes(abc, monkeypatch):
    """Defect: a foreign rewrite keeping inode, size and mtime goes unseen, or adopt-local drops its bytes."""
    from exomem.collection_store import runtime

    monkeypatch.setattr(runtime, "WATCH_SECONDS", 0.05)
    monkeypatch.setattr(runtime, "WATCH_DIGEST_SECONDS", 0.2)
    _published(abc)
    path = replica.replica_path(abc.root)
    signature, stamp = takeover.replica_signature(abc.root), path.stat()
    rewritten = bytearray(path.read_bytes())
    rewritten[-1] ^= 0xFF
    with open(path, "r+b") as file:
        file.write(rewritten)
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert takeover.replica_signature(abc.root) == signature
    _until(lambda: abc.manager.status()["collection_store"]["status"] == "diverged")
    refused(lambda: abc.write_c(LATER, "Refused"), "COLLECTION_STORE_DIVERGED")
    assert abc.read_c() == ["Canonical"] and abc.write_a("A beside divergence") == "committed"
    instance = abc.meta()[schema.META_INSTANCE_ID]
    preview, adopted = _adopt(abc, "the rewrite was a stray editor")
    evidence = [entry for entry in preview["fork_point"]["foreign"] if entry["source"] == "evidence"]
    assert [entry["sha256"] for entry in evidence] == [hashlib.sha256(rewritten).hexdigest()]
    assert adopted["status"] == "adopted", adopted.get("reason")
    assert abc.write_c(LATER, "After adopt")["outcome"] == "committed"
    assert (path.parent / evidence[0]["leaf"]).read_bytes() == rewritten
    meta = abc.meta()
    assert [fork["local"]["commit_seq"] for fork in json.loads(meta[schema.META_FORKS])] == [2]
    assert json.loads(meta[schema.META_LINEAGE])[-1]["adopted_from"] == instance
    assert _replica_meta(abc.root)[schema.META_INSTANCE_ID] == meta[schema.META_INSTANCE_ID]


def test_adopt_local_abandons_the_publication_a_divergence_interrupted(abc, monkeypatch):
    """Defect: a foreign write landing mid-publication leaves an intent that re-diverges every adopt-local."""
    from exomem.collection_store import runtime

    monkeypatch.setattr(runtime, "PUBLISH_INTERVAL_SECONDS", 0.1)  # the next write publishes promptly
    _published(abc)
    install = replica._Publisher.flush_installed

    def foreign_write_lands(publisher, expected):
        monkeypatch.setattr(replica._Publisher, "flush_installed", install)
        path = replica.replica_path(abc.root)
        path.write_bytes(path.read_bytes() + b"\0")
        return install(publisher, expected)

    monkeypatch.setattr(replica._Publisher, "flush_installed", foreign_write_lands)
    assert abc.write_c(LATER, "Mid publication")["outcome"] == "committed"
    _until(lambda: abc.manager.status()["collection_store"]["status"] == "diverged")
    assert schema.META_PENDING_REPLICA_PUBLICATION in abc.meta()
    preview, adopted = _adopt(abc, "the write mid-publication was a stray editor")
    assert adopted["status"] == "adopted", adopted.get("reason")
    assert preview["abandoned_publication"] is not None
    assert abc.write_c(str(uuid.uuid4()), "After adopt")["outcome"] == "committed"


@requires_fork
def test_reconcile_holds_every_foreign_item_change_after_the_fork_point(abc, tmp_path):
    """Defect: a diverged store loses the other side's later writes, or reconcile edits rows silently."""
    abc.release()  # the other host starts from A's flushed replica
    copy = tmp_path / "copy"
    shutil.copytree(abc.root, copy)

    def other_host(found):
        assert found.open() == {"status": "admitted"}
        found.write_c(LATER, "From the other host")
        found.release()

    run_host(tmp_path, tmp_path / "state-b", "host-b", other_host, root=copy,
             database=tmp_path / "copy-coordinator.sqlite", vault_id="copy")
    assert abc.write_c(str(uuid.uuid4()), "Kept on A")["outcome"] == "committed"
    _published(abc)
    shutil.copyfile(replica.replica_path(copy), abc.root / "delivery.tmp")
    os.replace(abc.root / "delivery.tmp", replica.replica_path(abc.root))  # sync delivers it
    _until(lambda: abc.manager.status()["collection_store"]["status"] == "diverged")
    _adopt(abc, "keep this host, hold the other")
    plan = admission.reconcile_store(abc.session, abc.manager, why="hold the other host's writes",
                                     fence_client=abc.operator)
    assert [(item["collection_id"], item["item_key"]) for item in plan["preview"]["items"]] == [(CID, LATER)]
    result = admission.reconcile_store(abc.session, abc.manager, why="hold the other host's writes",
                                       preview_id=plan["preview_id"], fence_client=abc.operator)
    assert result["status"] == "held" and abc.read_c() == ["Canonical", "Kept on A"]
    with closing(connection.open_reader(abc.session.path)) as reader:
        held = reader.execute("SELECT kind,code,held_bytes,diagnostics_json FROM held_candidates").fetchall()
    assert [(kind, code) for kind, code, _, _ in held] == [("view-correction", "COLLECTION_STORE_DIVERGED")]
    assert b"From the other host" in held[0][2] and json.loads(held[0][3])[0]["common_ancestor_commit_seq"] == 2
    again = admission.reconcile_store(abc.session, abc.manager, why="again", fence_client=abc.operator)
    assert again["preview"]["items"] == []


# --- later slices: strict xfails naming the slice that turns them green -----------------


@pytest.mark.xfail(strict=True, reason="s1-A3: compatibility flip for supported launchers on fresh roots")
def test_supported_runtime_admits_a_fresh_root_copy_of_the_c_vault(abc, tmp_path, monkeypatch):
    """Defect: the supported release itself can never start on a C vault copy or restore."""
    copy = tmp_path / "copy"
    shutil.copytree(abc.root, copy)
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "fresh-state"))
    assert "collections-store-v1" in state_migration.supported_state_compatibility_ids()
    state_migration.require_vault_state_ready(copy)


@pytest.mark.xfail(strict=True, reason="s1-A3: hosted snapshot export and staged restore admission")
def test_staged_restore_carries_the_replica_and_admits_the_c_vault(abc, tmp_path, monkeypatch):
    """Defect: a restore drops C's replica, carries live WAL bytes, or refuses the supported runtime."""
    from exomem import hosted_portability, hosted_restore

    abc.release()
    kb = "Knowledge Base/_Collections/"
    assert hosted_portability.classify_artifact(kb + "collections.sqlite").included
    assert not hosted_portability.classify_artifact(kb + "collections.sqlite-wal").included
    restored = tmp_path / "restored"
    shutil.copytree(abc.root, restored)
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "restored-state"))
    hosted_restore._require_restore_admission(restored)


@pytest.mark.xfail(strict=True, reason="s1-A3: slice rollback switch")
def test_slice_rollback_disables_c_but_keeps_its_data_and_a_b(abc):
    """Defect: rolling back the slice deletes C's routing/rows/replica or disables A/B."""
    marker = authority.read_marker(abc.root)
    admission.rollback_slice(abc.session, abc.manager)
    refused(abc.read_c, "COLLECTION_STORE_DISABLED")
    assert authority.read_marker(abc.root) == marker
    assert abc.meta()[schema.META_COMMIT_SEQ] == "2"
    assert abc.write_a("A after rollback") == "committed"
