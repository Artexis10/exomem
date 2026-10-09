"""Replica outcomes survive real filesystem and bookkeeping interruptions."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from exomem import held_fs, reserved_paths
from exomem.cli_ops import OpError
from exomem.collection_store import connection, replica, schema, snapshot, tokens
from exomem.kbdir import kb_dirname


@pytest.fixture
def store(tmp_path: Path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / kb_dirname()).mkdir()
    with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as writer:
        yield root, writer


def _publish(root, writer, **kwargs):
    return replica.publish_replica(
        root, writer, authority_check=lambda: True, deadline=time.monotonic() + 10, **kwargs
    )


def _metadata(writer):
    return dict(writer.connection.execute("SELECT key,value FROM store_meta"))


def _commit(writer):
    metadata = _metadata(writer)
    sequence = int(metadata[schema.META_COMMIT_SEQ]) + 1
    event = "a" * 64
    head = tokens.store_head_hash(metadata.get(schema.META_STORE_HEAD_HASH), sequence, event)
    with writer.transaction() as conn:
        conn.execute(
            "INSERT INTO txns(txn_id,transition_id,collection_id,operation,generation_before,"
            "generation_after,actor,why,receipt_json,committed_at,event_hash,commit_seq,"
            "store_head_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sequence,
                f"{sequence:024x}",
                "00000000-0000-4000-8000-000000000001",
                "append",
                sequence - 1,
                sequence,
                "owner",
                "invented",
                "{}",
                "2026-10-04T00:00:00Z",
                event,
                sequence,
                head,
            ),
        )
    return sequence, head


def test_first_and_successive_publish_are_reopenable_business_snapshots(store):
    root, writer = store
    target = replica.replica_path(root)
    for sequence in range(2):
        expected = _commit(writer) if sequence else (0, None)
        result = _publish(root, writer)
        assert target.is_file()
        assert result.status == "published"
        assert (result.commit_seq, result.head_hash) == expected
        with closing(connection.open_reader(target)) as reader:
            assert reader.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert reader.execute("PRAGMA journal_mode").fetchone() == ("delete",)
            copied = dict(reader.execute("SELECT key,value FROM store_meta"))
            assert (
                int(copied[schema.META_COMMIT_SEQ]),
                copied.get(schema.META_STORE_HEAD_HASH),
            ) == expected
        metadata = _metadata(writer)
        assert (
            int(metadata[schema.META_COMMIT_SEQ]),
            metadata.get(schema.META_STORE_HEAD_HASH),
        ) == expected
        assert (
            metadata[schema.META_LAST_PUBLISHED_REPLICA_SHA256]
            == hashlib.sha256(target.read_bytes()).hexdigest()
        )
        assert schema.META_PENDING_REPLICA_PUBLICATION not in metadata
        digest = metadata[schema.META_LAST_PUBLISHED_REPLICA_SHA256]
        assert _publish(root, writer).status == "published"
        assert hashlib.sha256(target.read_bytes()).hexdigest() == digest
        assert not any(
            target.with_name(target.name + suffix).exists()
            for suffix in ("-wal", "-shm", "-journal")
        )


@pytest.mark.parametrize("concurrent", [False, True])
def test_matching_installed_copy_retires_the_still_owned_redundant_stage(
    store, monkeypatch, concurrent
):
    root, writer = store
    target = replica.replica_path(root)
    original = snapshot.staged_snapshot
    workspace = None

    @contextmanager
    def matching_copy_arrives(*args, **kwargs):
        nonlocal workspace
        with original(*args, **kwargs) as artifact:
            workspace = artifact.path.parent
            target.write_bytes(artifact.path.read_bytes())
            yield artifact

    monkeypatch.setattr(snapshot, "staged_snapshot", matching_copy_arrives)
    if concurrent:
        @contextmanager
        def step(_patience, _cancelled):
            yield writer, lambda: True

        result = replica.publish_replica_concurrently(
            root, step=step, deadline=time.monotonic() + 10
        )
    else:
        result = _publish(root, writer)
    assert result.status == "published", result
    assert (result.commit_seq, result.head_hash) == (0, None)
    assert not workspace.exists()
    metadata = _metadata(writer)
    assert schema.META_PENDING_REPLICA_PUBLICATION not in metadata
    assert metadata[schema.META_LAST_PUBLISHED_REPLICA_SHA256] == hashlib.sha256(
        target.read_bytes()
    ).hexdigest()
    with closing(connection.open_reader(target)) as reader:
        assert reader.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def _filesystem_type(root):
    with held_fs.acquire(root).require() as filesystem:
        return type(filesystem)


def _recover(root, writer):
    return replica.recover_replica(
        root, writer, authority_check=lambda: True, deadline=time.monotonic() + 10
    )


@pytest.mark.parametrize("after_install", [False, True])
def test_install_crash_reopens_without_foreign_divergence(store, monkeypatch, after_install):
    root, writer = store
    target = replica.replica_path(root)
    filesystem_type = _filesystem_type(root)
    original = filesystem_type.rename

    def interrupted(filesystem, source, parent, leaf, **kwargs):
        if leaf == target.name and not after_install:
            raise SystemExit("crash before install")
        result = original(filesystem, source, parent, leaf, **kwargs)
        if leaf == target.name:
            raise SystemExit("crash after install")
        return result

    with monkeypatch.context() as injection:
        injection.setattr(filesystem_type, "rename", interrupted)
        with pytest.raises(SystemExit):
            _publish(root, writer)
    metadata = _metadata(writer)
    assert schema.META_PENDING_REPLICA_PUBLICATION in metadata
    assert schema.META_LAST_PUBLISHED_REPLICA_SHA256 not in metadata
    assert target.exists() == after_install
    writer.close()
    with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as reopened:
        assert _publish(root, reopened).status == "published"
        assert schema.META_REPLICA_DIVERGENCE not in _metadata(reopened)
        with closing(connection.open_reader(target)) as reader:
            assert reader.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_displacement_crash_with_missing_stage_restores_predecessor_then_flushes_new_head(
    store, monkeypatch
):
    root, writer = store
    assert _publish(root, writer).status == "published"
    target = replica.replica_path(root)
    old_digest = hashlib.sha256(target.read_bytes()).hexdigest()
    expected = _commit(writer)
    filesystem_type = _filesystem_type(root)
    original = filesystem_type.rename

    def interrupted(filesystem, source, parent, leaf, **kwargs):
        result = original(filesystem, source, parent, leaf, **kwargs)
        if "replica-aside" in leaf:
            raise SystemExit("crash after displacement")
        return result

    with monkeypatch.context() as injection:
        injection.setattr(filesystem_type, "rename", interrupted)
        with pytest.raises(SystemExit):
            _publish(root, writer)
    pending = json.loads(_metadata(writer)[schema.META_PENDING_REPLICA_PUBLICATION])
    assert not target.exists()
    assert not (target.parent / pending["workspace_leaf"] / pending["stage_leaf"]).exists()
    assert _metadata(writer)[schema.META_LAST_PUBLISHED_REPLICA_SHA256] == old_digest
    restored = _recover(root, writer)
    assert (restored.commit_seq, restored.head_hash) == (0, None)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == old_digest
    result = _publish(root, writer)
    assert result.status == "published"
    assert (result.commit_seq, result.head_hash) == expected


def test_foreign_replacement_is_protected_and_business_writes_are_fenced(store):
    root, writer = store
    assert _publish(root, writer).status == "published"
    target = replica.replica_path(root)
    _commit(writer)
    target.write_bytes(b"foreign replica")
    result = _publish(root, writer)
    assert result.status == "diverged"
    preserved = list(target.parent.glob(".foreign-*"))
    assert len(preserved) == 1
    assert preserved[0].read_bytes() == b"foreign replica"
    assert schema.META_REPLICA_DIVERGENCE in _metadata(writer)
    with pytest.raises(connection.CollectionStoreError, match="COLLECTION_STORE_DIVERGED"):
        _commit(writer)
    with closing(connection.open_reader(writer.path)) as reader:
        assert reader.execute("SELECT count(*) FROM txns").fetchone() == (1,)


@pytest.mark.parametrize("collision", ["target", "capture"])
def test_no_clobber_competitor_at_shared_install_or_capture_is_retained(
    store, monkeypatch, collision
):
    root, writer = store
    assert _publish(root, writer).status == "published"
    target = replica.replica_path(root)
    _commit(writer)
    filesystem_type = _filesystem_type(root)
    original = filesystem_type.rename
    injected = False

    def race(filesystem, source, parent, leaf, **kwargs):
        nonlocal injected
        if not injected and (
            (collision == "target" and leaf == target.name)
            or (collision == "capture" and "replica-aside" in leaf)
        ):
            injected = True
            target.write_bytes(b"racing replica")
        return original(filesystem, source, parent, leaf, **kwargs)

    with monkeypatch.context() as injection:
        injection.setattr(filesystem_type, "rename", race)
        result = _publish(root, writer)
    assert injected and result.status == "diverged"
    assert any(
        path.read_bytes() == b"racing replica" for path in target.parent.iterdir() if path.is_file()
    )
    assert schema.META_REPLICA_DIVERGENCE in _metadata(writer)


@pytest.mark.skipif(
    os.name != "posix", reason="native displacement race uses the Linux rename seam"
)
@pytest.mark.parametrize("preservation_blocked", [False, True])
def test_rehashes_displaced_name_after_sync_replaces_prechecked_inode(
    store, monkeypatch, preservation_blocked
):
    root, writer = store
    assert _publish(root, writer).status == "published"
    target = replica.replica_path(root)
    _commit(writer)
    backend = held_fs._backend()
    original = backend._rename_noreplace
    replaced = False

    def race(source, destination, **kwargs):
        nonlocal replaced
        if preservation_blocked and destination.startswith(".foreign-"):
            raise PermissionError("foreign relocation is temporarily blocked")
        if source == target.name and not replaced:
            replaced = True
            competitor = target.with_name("incoming")
            competitor.write_bytes(b"displaced foreign inode")
            competitor.replace(target)
        return original(source, destination, **kwargs)

    with monkeypatch.context() as injection:
        injection.setattr(backend, "_rename_noreplace", race)
        result = _publish(root, writer)
    assert replaced and result.status == "diverged"
    if preservation_blocked:
        pending = json.loads(_metadata(writer)[schema.META_PENDING_REPLICA_PUBLICATION])
        marker = json.loads(_metadata(writer)[schema.META_REPLICA_DIVERGENCE])
        retained = root / marker["source_leaf"]
        assert retained.parent.name == pending["workspace_leaf"]
        assert retained.read_bytes() == b"displaced foreign inode"
        assert "preservation pending" in result.reason
    else:
        assert any(
            path.read_bytes() == b"displaced foreign inode"
            for path in target.parent.iterdir()
            if path.is_file()
        )


@pytest.mark.parametrize("failure", ["cancel", "authority", "lease", "sharing"])
def test_interruption_after_intent_remains_retryable(store, monkeypatch, failure):
    root, writer = store
    target = replica.replica_path(root)
    filesystem_type = _filesystem_type(root)
    original = filesystem_type.rename
    interrupted = False

    def stop(filesystem, source, parent, leaf, **kwargs):
        nonlocal interrupted
        if leaf == target.name:
            interrupted = True
            if failure == "sharing":
                return held_fs.HeldResult(
                    error=held_fs.HeldFsError("IO_REFUSED", "sharing violation")
                )
        return original(filesystem, source, parent, leaf, **kwargs)

    def allowed():
        return not (interrupted and failure == "authority")

    with monkeypatch.context() as injection:
        injection.setattr(filesystem_type, "rename", stop)
        if failure == "lease":
            injection.setattr(writer, "_lease_check", lambda: not interrupted)
        result = replica.publish_replica(
            root,
            writer,
            authority_check=allowed,
            deadline=time.monotonic() + 10,
            cancelled=lambda: interrupted and failure == "cancel",
        )
    assert interrupted and result.status == "retry_pending"
    assert schema.META_PENDING_REPLICA_PUBLICATION in _metadata(writer)
    assert schema.META_REPLICA_DIVERGENCE not in _metadata(writer)
    assert _publish(root, writer).status == "published"


def _clock(injection):
    """Move the monotonic clock forward; the snapshot and the publisher both read it."""
    monotonic, offset = time.monotonic, [0.0]
    injection.setattr(time, "monotonic", lambda: monotonic() + offset[0])

    def advance(seconds):
        offset[0] += seconds

    return advance


def _on_copied_integrity_check(injection, effect):
    """Run ``effect`` as the copied store's integrity check starts.

    Returns the statements SQLite interrupted: a stop inside the check, not at the
    phase boundary after it, is what keeps a long validation bounded.
    """
    connect = sqlite3.connect
    interrupted = []

    class CopiedStore(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == "PRAGMA integrity_check":
                effect()
            try:
                return super().execute(sql, *args, **kwargs)
            except sqlite3.OperationalError:
                interrupted.append(sql)
                raise

    def copied(database, *args, **kwargs):
        if Path(database).name.startswith(snapshot._PREFIX):
            kwargs["factory"] = CopiedStore
        return connect(database, *args, **kwargs)

    injection.setattr(sqlite3, "connect", copied)
    return interrupted


def test_lost_custody_stops_the_copy_inside_its_integrity_check(store, monkeypatch):
    # The custody recheck is paced by time, not by SQL work; it must still stop a long check.
    root, writer = store
    for _ in range(40):  # history the integrity check takes several progress-handler steps over
        _commit(writer)
    custody = [True]
    with monkeypatch.context() as injection:
        advance = _clock(injection)

        def lose_custody():
            custody[0] = False
            advance(snapshot._RECHECK_SECONDS)

        interrupted = _on_copied_integrity_check(injection, lose_custody)
        result = replica.publish_replica(
            root, writer, authority_check=lambda: custody[0], deadline=time.monotonic() + 10
        )
    assert interrupted == ["PRAGMA integrity_check"]
    assert result.status == "retry_pending" and result.reason == "publication authority was lost"
    assert not replica.replica_path(root).exists()
    assert _publish(root, writer).status == "published"


def test_changed_fencing_token_stops_the_copy_inside_its_integrity_check(store, monkeypatch):
    # A fenced writer's lease check raises its own error; SQLite must not swallow it as corruption.
    root, writer = store
    for _ in range(40):  # history the integrity check takes several progress-handler steps over
        _commit(writer)
    fenced = [False]

    def lease_check():
        if fenced[0]:
            raise OpError("WRITER_FENCED", "writer lease fencing token 1 is no longer current")
        return True

    with monkeypatch.context() as injection:
        injection.setattr(writer, "_lease_check", lease_check)
        advance = _clock(injection)

        def fence():
            fenced[0] = True
            advance(snapshot._RECHECK_SECONDS)

        interrupted = _on_copied_integrity_check(injection, fence)
        with pytest.raises(OpError, match="WRITER_FENCED"):
            _publish(root, writer)
    assert interrupted == ["PRAGMA integrity_check"]
    assert not replica.replica_path(root).exists()
    assert _publish(root, writer).status == "published"


def test_deadline_inside_the_copied_integrity_check_is_a_retryable_timeout(store, monkeypatch):
    # Running out of time says nothing about the copy: never COLLECTION_SNAPSHOT_INVALID.
    root, writer = store
    for _ in range(40):  # history the integrity check takes several progress-handler steps over
        _commit(writer)
    with monkeypatch.context() as injection:
        advance = _clock(injection)
        interrupted = _on_copied_integrity_check(injection, lambda: advance(60))
        result = _publish(root, writer)
    assert interrupted == ["PRAGMA integrity_check"]
    assert result.status == "retry_pending"
    assert result.reason.startswith("COLLECTION_SNAPSHOT_DEADLINE")
    assert schema.META_PENDING_REPLICA_PUBLICATION in _metadata(writer)
    assert _publish(root, writer).status == "published"


def test_recovery_reports_old_head_and_flush_reaches_later_committed_head(store, monkeypatch):
    root, writer = store
    filesystem_type = _filesystem_type(root)
    original = filesystem_type.flush_directory

    def stop_after_install(filesystem, parent):
        if replica.replica_path(root).exists():
            return held_fs.HeldResult(error=held_fs.HeldFsError("IO_REFUSED", "sharing violation"))
        return original(filesystem, parent)

    with monkeypatch.context() as injection:
        injection.setattr(filesystem_type, "flush_directory", stop_after_install)
        assert _publish(root, writer).status == "retry_pending"
    expected = _commit(writer)
    result = _recover(root, writer)
    assert result.status == "published" and (result.commit_seq, result.head_hash) == (0, None)
    result = _publish(root, writer)
    assert result.status == "published" and (result.commit_seq, result.head_hash) == expected


def test_replica_tree_and_standalone_snapshot_share_owner_without_generic_access(store):
    root, writer = store
    standalone = ".exomem-collection-snapshot-" + "a" * 32 + ".sqlite"
    for path, descriptor in (
        ("_COLLECTIONS/" + standalone, "collection-replica"),
        ("Records/" + standalone, "collection-snapshot"),
    ):
        assert reserved_paths.classify_logical(path).descriptor_id == descriptor
        with reserved_paths._subsystem_authority_scope("collection_store.replica"):
            assert reserved_paths.owner_authorized(descriptor)
    assert _publish(root, writer).status == "published"
    with pytest.raises(reserved_paths.ReservedPathLeafError):
        reserved_paths.read_generic_bytes(root, str(replica.replica_path(root).relative_to(root)))


def test_hard_process_crash_resumes_verified_stage_after_displacement(store):
    root, writer = store
    assert _publish(root, writer).status == "published"
    expected = _commit(writer)
    target = replica.replica_path(root)
    writer.close()
    script = """
import os, sys, time
from pathlib import Path
from exomem import held_fs
from exomem.collection_store import connection, replica
root = Path(sys.argv[1])
with held_fs.acquire(root).require() as fs:
    filesystem_type = type(fs)
original = filesystem_type.rename
def crash(fs, source, parent, leaf, **kwargs):
    result = original(fs, source, parent, leaf, **kwargs)
    if 'replica-aside' in leaf:
        os._exit(73)
    return result
filesystem_type.rename = crash
with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as writer:
    replica.publish_replica(root, writer, authority_check=lambda: True, deadline=time.monotonic()+10)
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(root)], capture_output=True, timeout=15
    )
    assert child.returncode == 73, child.stderr.decode()
    with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as reopened:
        pending = json.loads(_metadata(reopened)[schema.META_PENDING_REPLICA_PUBLICATION])
        workspace = target.parent / pending["workspace_leaf"]
        assert not target.exists() and (workspace / pending["stage_leaf"]).exists()
        result = _recover(root, reopened)
        assert result.status == "published" and (result.commit_seq, result.head_hash) == expected
        assert not list(target.parent.glob(".exomem-collection-*"))
        with closing(connection.open_reader(target)) as reader:
            assert reader.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def test_changed_epoch_leaves_durable_intent_retryable(store, monkeypatch):
    root, writer = store
    filesystem_type = _filesystem_type(root)
    original = filesystem_type.rename

    def sharing(filesystem, source, parent, leaf, **kwargs):
        if leaf == connection.STORE_FILENAME:
            return held_fs.HeldResult(error=held_fs.HeldFsError("IO_REFUSED", "sharing violation"))
        return original(filesystem, source, parent, leaf, **kwargs)

    with monkeypatch.context() as injection:
        injection.setattr(filesystem_type, "rename", sharing)
        assert _publish(root, writer).status == "retry_pending"
    pending = _metadata(writer)[schema.META_PENDING_REPLICA_PUBLICATION]
    with writer.transaction() as conn:
        conn.execute("INSERT INTO store_meta VALUES (?,?)", (schema.META_LEASE_EPOCH, "new-epoch"))
    result = _recover(root, writer)
    assert result.status == "retry_pending"
    assert _metadata(writer)[schema.META_PENDING_REPLICA_PUBLICATION] == pending
    assert schema.META_REPLICA_DIVERGENCE not in _metadata(writer)


@pytest.mark.parametrize("invalid", ["stage", "head"])
def test_invalid_intent_cannot_delete_unowned_content_or_publish_nonancestor(store, invalid):
    root, writer = store
    victim = root / "owner.md"
    victim.write_bytes(b"owner content")
    metadata = _metadata(writer)
    pending = {
        "version": 2,
        "phase": "ready",
        "token": "a" * 32,
        "workspace_leaf": ".exomem-collection-replica-work-" + "a" * 32,
        "workspace_identity": {"device": 1, "inode": 1, "kind": "directory", "link_count": 2},
        "stage_leaf": "../owner.md"
        if invalid == "stage"
        else ".exomem-collection-snapshot-" + "a" * 32 + ".sqlite",
        "sha256": hashlib.sha256(victim.read_bytes()).hexdigest(),
        "store_id": metadata[schema.META_STORE_ID],
        "instance_id": metadata[schema.META_INSTANCE_ID],
        "commit_seq": 1 if invalid == "head" else 0,
        "head_hash": "c" * 64 if invalid == "head" else None,
        "lease_epoch": None,
    }
    with writer.transaction() as conn:
        conn.execute(
            "INSERT INTO store_meta VALUES (?,?)",
            (schema.META_PENDING_REPLICA_PUBLICATION, json.dumps(pending)),
        )
    assert _recover(root, writer).status == "diverged"
    assert victim.read_bytes() == b"owner content"
    assert not replica.replica_path(root).exists()


@pytest.mark.parametrize("failure", ["authority", "lease", "sharing"])
def test_foreign_preservation_failure_keeps_bytes_and_durable_divergence(
    store, monkeypatch, failure
):
    root, writer = store
    assert _publish(root, writer).status == "published"
    target = replica.replica_path(root)
    target.write_bytes(b"foreign requiring preservation")

    def diverged():
        return schema.META_REPLICA_DIVERGENCE in _metadata(writer)

    filesystem_type = _filesystem_type(root)
    original = filesystem_type.rename

    def sharing(filesystem, source, parent, leaf, **kwargs):
        if leaf.startswith(".foreign-"):
            return held_fs.HeldResult(error=held_fs.HeldFsError("IO_REFUSED", "sharing violation"))
        return original(filesystem, source, parent, leaf, **kwargs)

    with monkeypatch.context() as injection:
        if failure == "lease":
            injection.setattr(writer, "_lease_check", lambda: not diverged())
        if failure == "sharing":
            injection.setattr(filesystem_type, "rename", sharing)
        result = replica.publish_replica(
            root,
            writer,
            authority_check=lambda: not (failure == "authority" and diverged()),
            deadline=time.monotonic() + 10,
        )
    assert result.status == "diverged" and "preservation pending" in result.reason
    assert target.read_bytes() == b"foreign requiring preservation"
    assert diverged()
    assert _publish(root, writer).status == "diverged"


def test_callback_refusal_and_wrong_vault_binding_have_no_publication_effect(store, tmp_path):
    root, writer = store
    before = _metadata(writer)
    result = replica.publish_replica(
        root, writer, authority_check=lambda: False, deadline=time.monotonic() + 10
    )
    assert result.status == "retry_pending" and _metadata(writer) == before
    assert not replica.replica_path(root).parent.exists()
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(connection.CollectionStoreError, match="COLLECTION_STORE_VAULT_MISMATCH"):
        _publish(other, writer)
    assert not replica.replica_path(other).parent.exists()


def test_missing_knowledge_base_is_retryable_without_bootstrapping(store):
    root, writer = store
    (root / kb_dirname()).rename(root / "saved-kb")
    metadata = _metadata(writer)
    assert _publish(root, writer).status == "retry_pending"
    assert _metadata(writer) == metadata
    assert not (root / kb_dirname()).exists()


def test_failed_foreign_stage_relocation_cannot_delete_changed_bytes_on_context_exit(
    store, monkeypatch
):
    # Defensive guard for out-of-model private mutation, not shared sync input.
    root, writer = store
    original = snapshot.staged_snapshot
    changed = None

    @contextmanager
    def mutate_after_validation(*args, **kwargs):
        nonlocal changed
        with original(*args, **kwargs) as artifact:
            changed = artifact.path
            changed.write_bytes(b"foreign content in the original stage inode")
            yield artifact

    monkeypatch.setattr(snapshot, "staged_snapshot", mutate_after_validation)
    result = replica.publish_replica(
        root,
        writer,
        authority_check=lambda: schema.META_REPLICA_DIVERGENCE not in _metadata(writer),
        deadline=time.monotonic() + 10,
    )
    assert result.status == "diverged" and "preservation pending" in result.reason
    assert changed.read_bytes() == b"foreign content in the original stage inode"


@pytest.mark.skipif(os.name != "posix", reason="shared-name unlink race uses POSIX descriptors")
def test_retirement_never_unlinks_a_shared_name_that_sync_can_replace(store, monkeypatch):
    root, writer = store
    assert _publish(root, writer).status == "published"
    _commit(writer)
    target = replica.replica_path(root)
    backend = held_fs._backend()
    original = backend._unlink
    shared_inode = target.parent.stat().st_ino
    foreign = b"sync input arriving between ownership check and unlink"
    shared_unlinks = []

    def arriving(name, **kwargs):
        if os.fstat(kwargs["dir_fd"]).st_ino == shared_inode and str(name).startswith(
            (".exomem-collection-snapshot-", ".exomem-collection-replica-aside-")
        ):
            shared_unlinks.append(name)
            incoming = target.with_name("incoming")
            incoming.write_bytes(foreign)
            incoming.replace(target.with_name(name))
        return original(name, **kwargs)

    monkeypatch.setattr(backend, "_unlink", arriving)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {arriving})
    result = _publish(root, writer)
    assert result.status == "published", result
    assert not shared_unlinks, "sync-replaceable names must never be destructively retired"


@pytest.mark.parametrize("pending", [False, True])
def test_matching_replacement_requires_actual_file_flush_before_success(
    store, monkeypatch, pending
):
    root, writer = store
    original = replica._Publisher.bookkeeping

    def before_promotion(publisher, values):
        if schema.META_PUBLISHED_REPLICA_HEAD in values:
            raise SystemExit("installed but not promoted")
        return original(publisher, values)

    with monkeypatch.context() as injection:
        if pending:
            injection.setattr(replica._Publisher, "bookkeeping", before_promotion)
            with pytest.raises(SystemExit):
                _publish(root, writer)
        else:
            assert _publish(root, writer).status == "published"
    target = replica.replica_path(root)
    incoming = target.with_name("incoming")
    incoming.write_bytes(target.read_bytes())
    incoming.replace(target)
    inode = target.stat().st_ino
    before = _metadata(writer)
    original_fsync = os.fsync
    attempted = False

    def fail_actual_file(descriptor):
        nonlocal attempted
        if os.fstat(descriptor).st_ino == inode:
            attempted = True
            raise OSError("installed replacement file flush failed")
        return original_fsync(descriptor)

    with monkeypatch.context() as injection:
        injection.setattr(os, "fsync", fail_actual_file)
        result = _recover(root, writer) if pending else _publish(root, writer)
    assert result.status == "retry_pending" and attempted
    assert _metadata(writer) == before
    assert (_recover(root, writer) if pending else _publish(root, writer)).status == "published"


def _crash_staging(root, boundary):
    script = """
import os, sys, time
from pathlib import Path
from contextlib import contextmanager
from exomem import held_fs
from exomem.collection_store import connection, replica, snapshot
root, boundary = Path(sys.argv[1]), sys.argv[2]
with held_fs.acquire(root).require() as fs:
    filesystem_type = type(fs)
original_parent = filesystem_type.parent
def parent(fs, relative, **kwargs):
    allocating = kwargs.get('exclusive') and kwargs.get('create')
    if allocating and boundary == 'intent':
        os._exit(73)
    result = original_parent(fs, relative, **kwargs)
    if allocating and boundary == 'allocation':
        os._exit(73)
    return result
filesystem_type.parent = parent
original_snapshot = snapshot.staged_snapshot
@contextmanager
def staged(*args, **kwargs):
    # Old publication has no operation directory; still die before backup.
    if boundary != 'ready':
        os._exit(73)
    with original_snapshot(*args, **kwargs) as artifact:
        os._exit(73)
        yield artifact
snapshot.staged_snapshot = staged
with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as writer:
    replica.publish_replica(root, writer, authority_check=lambda: True, deadline=time.monotonic()+10)
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(root), boundary], capture_output=True, timeout=15
    )
    assert child.returncode == 73, child.stderr.decode()


@pytest.mark.parametrize("boundary", ["intent", "allocation", "identity", "ready"])
def test_hard_death_before_ready_reclaims_only_recorded_staging(store, boundary):
    root, writer = store
    writer.close()
    _crash_staging(root, boundary)
    with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as reopened:
        pending = json.loads(_metadata(reopened)[schema.META_PENDING_REPLICA_PUBLICATION])
        assert pending["phase"] == "staging" and "sha256" not in pending
        assert "head_hash" not in pending and "commit_seq" not in pending
        workspace = replica.replica_path(root).parent / pending["workspace_leaf"]
        assert workspace.exists() == (boundary != "intent")
        assert (pending["workspace_identity"] is None) == (boundary in {"intent", "allocation"})
        if boundary == "identity":
            assert not list(workspace.iterdir())
        if boundary == "ready":
            assert (workspace / pending["stage_leaf"]).is_file()
        result = _recover(root, reopened)
        assert result.status == "retry_pending"
        assert schema.META_PENDING_REPLICA_PUBLICATION not in _metadata(reopened)
        assert not list(replica.replica_path(root).parent.glob(".exomem-collection-*"))
        assert _publish(root, reopened).status == "published"


@pytest.mark.parametrize("unexpected", ["unrecorded", "unknown", "identity"])
def test_unresolved_staging_preserves_workspace_without_allocating_again(store, unexpected):
    root, writer = store
    writer.close()
    _crash_staging(root, "allocation" if unexpected == "unrecorded" else "ready")
    with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as reopened:
        raw = _metadata(reopened)[schema.META_PENDING_REPLICA_PUBLICATION]
        pending = json.loads(raw)
        workspace = replica.replica_path(root).parent / pending["workspace_leaf"]
        if unexpected == "identity":
            workspace.rename(workspace.with_name("original-workspace"))
            workspace.mkdir()
        victim = workspace / (pending["stage_leaf"] if unexpected == "identity" else "owner-input")
        victim.write_bytes(b"unknown content")
        before = set(workspace.parent.iterdir())
        assert _publish(root, reopened).status == "retry_pending"
        assert victim.read_bytes() == b"unknown content"
        assert _metadata(reopened)[schema.META_PENDING_REPLICA_PUBLICATION] == raw
        assert set(workspace.parent.iterdir()) == before


@pytest.mark.parametrize("cut", ["file", "directory"])
def test_staging_cleanup_interruption_keeps_intent_until_directory_removal_is_durable(
    store, monkeypatch, cut
):
    root, writer = store
    writer.close()
    _crash_staging(root, "ready")
    with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as reopened:
        raw = _metadata(reopened)[schema.META_PENDING_REPLICA_PUBLICATION]
        filesystem_type = _filesystem_type(root)
        method = "unlink" if cut == "file" else "unlink_directory"
        original = getattr(filesystem_type, method)

        def interrupted(filesystem, directory):
            original(filesystem, directory).require()
            raise SystemExit("cleanup interrupted before clearing intent")

        with monkeypatch.context() as injection:
            injection.setattr(filesystem_type, method, interrupted)
            with pytest.raises(SystemExit):
                _recover(root, reopened)
        assert _metadata(reopened)[schema.META_PENDING_REPLICA_PUBLICATION] == raw
        assert _recover(root, reopened).status == "retry_pending"
        assert schema.META_PENDING_REPLICA_PUBLICATION not in _metadata(reopened)
