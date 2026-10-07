"""Staged snapshots own scratch, not publication or the live writer."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from exomem import held_fs, reserved_paths
from exomem.collection_store import connection, schema, snapshot, tokens

COLLECTION_ID = "00000000-0000-4000-8000-000000000001"
H = "a" * 64


@pytest.fixture
def genesis(tmp_path: Path):
    root = tmp_path / "vault"
    root.mkdir()
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as writer:
        yield root, stage, writer


def _commit(writer: connection.WriterConnection, *, initial: bool = False) -> str:
    with writer.transaction() as conn:
        seq = (
            int(conn.execute("SELECT value FROM store_meta WHERE key='commit_seq'").fetchone()[0])
            + 1
        )
        previous = conn.execute(
            "SELECT value FROM store_meta WHERE key='store_head_hash'"
        ).fetchone()
        head = tokens.store_head_hash(previous[0] if previous else None, seq, H)
        conn.execute(
            "INSERT INTO txns(txn_id,transition_id,collection_id,operation,generation_before,"
            "generation_after,actor,why,receipt_json,committed_at,event_hash,commit_seq,"
            "store_head_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                seq,
                f"{seq:024x}",
                COLLECTION_ID,
                "append",
                seq - 1,
                seq,
                "owner",
                "invented",
                "{}",
                "2026-09-30T00:00:00Z",
                H,
                seq,
                head,
            ),
        )
        if initial:
            conn.execute("INSERT INTO collection_types VALUES ('records',1,1)")
            conn.execute(
                "INSERT INTO collection_type_versions(name,version,declaration_json,"
                "declaration_hash,change_class,txn_id) VALUES ('records',1,'{}',?,'builtin',1)",
                (H,),
            )
            conn.execute(
                "INSERT INTO collection_manifests(collection_id,manifest_version,manifest_text,"
                "manifest_hash,schema_json,txn_id) VALUES (?,1,'invented',?,'{}',1)",
                (COLLECTION_ID, H),
            )
            conn.execute(
                "INSERT INTO collections(collection_id,type_name,type_version,manifest_path,"
                "source_path,layout,manifest_version,generation,audit_reader_version,"
                "created_txn,updated_txn) VALUES (?,'records',1,'manifest.md','items',"
                "'markdown-items',1,1,2,1,1)",
                (COLLECTION_ID,),
            )
            conn.executemany(
                "INSERT INTO items(collection_id,item_key,row_version,schema_version,values_json,"
                "payload_hash,view_path,created_txn,updated_txn) VALUES (?,?,1,1,?,?,?,1,1)",
                (
                    (COLLECTION_ID, str(i), json.dumps({"value": "x" * 200}), H, f"items/{i}.md")
                    for i in range(10_000)
                ),
            )
        else:
            conn.execute("UPDATE items SET row_version=2,updated_txn=?", (seq,))
    return head


def test_snapshot_pins_one_wal_boundary_and_releases_reader_before_yield(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    live = connection.store_path(root)
    with connection.open_writer(live, lease_check=lambda: True) as writer:
        before_head = _commit(writer, initial=True)
        expected = dict(writer.connection.execute("SELECT key,value FROM store_meta"))
    copying = threading.Event()
    committed = threading.Event()
    result = {}

    def write_during_copy():
        try:
            with connection.open_writer(live, lease_check=lambda: True) as writer:
                assert copying.wait(10)
                result["head"] = _commit(writer)
                committed.set()
                assert result["consumer"].wait(10)
                result["checkpoint"] = writer.connection.execute(
                    "PRAGMA wal_checkpoint(TRUNCATE)"
                ).fetchone()
        except (AssertionError, sqlite3.Error, connection.CollectionStoreError) as error:
            result["error"] = error
            committed.set()

    class ObservedReader(sqlite3.Connection):
        def backup(self, target, **kwargs):
            progress = kwargs["progress"]

            def observe(status, remaining, total):
                if status == sqlite3.SQLITE_OK and remaining and not copying.is_set():
                    copying.set()
                    assert committed.wait(10)
                    if "error" in result:
                        raise result["error"]
                progress(status, remaining, total)

            super().backup(target, **{**kwargs, "progress": observe})

    result["consumer"] = threading.Event()
    thread = threading.Thread(target=write_during_copy)
    thread.start()
    monkeypatch.setattr(connection, "_CONNECTION_FACTORY", ObservedReader)
    try:
        with snapshot.staged_snapshot(
            root, directory=stage, deadline=time.monotonic() + 20
        ) as artifact:
            assert copying.is_set()
            assert artifact.commit_seq == 1
            assert artifact.head_hash == before_head
            assert artifact.store_id == expected[schema.META_STORE_ID]
            assert artifact.instance_id == expected[schema.META_INSTANCE_ID]
            assert artifact.lineage_json == expected[schema.META_LINEAGE]
            assert artifact.lease_epoch is None
            assert artifact.schema_version == schema.SCHEMA_VERSION
            assert artifact.size_bytes == artifact.path.stat().st_size
            assert artifact.file_sha256 == hashlib.sha256(artifact.path.read_bytes()).hexdigest()
            with closing(sqlite3.connect(artifact.path)) as copied:
                assert copied.execute("PRAGMA journal_mode").fetchone() == ("delete",)
                assert copied.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
                assert copied.execute("PRAGMA foreign_key_check").fetchall() == []
                assert copied.execute(
                    "SELECT count(*),min(row_version),max(row_version) FROM items"
                ).fetchone() == (10_000, 1, 1)
                assert copied.execute("SELECT commit_seq,store_head_hash FROM txns").fetchall() == [
                    (1, before_head)
                ]
            assert list(stage.iterdir()) == [artifact.path]
            with pytest.raises(FrozenInstanceError):
                artifact.commit_seq = 2
            result["consumer"].set()
            thread.join(10)
            assert not thread.is_alive()
            assert "error" not in result
            assert result["checkpoint"] == (0, 0, 0)
        assert list(stage.iterdir()) == []
        with closing(connection.open_reader(live)) as reader:
            assert reader.execute("SELECT count(*) FROM items WHERE row_version=2").fetchone() == (
                10_000,
            )
            assert reader.execute(
                "SELECT value FROM store_meta WHERE key='store_head_hash'"
            ).fetchone() == (result["head"],)
    finally:
        copying.set()
        result["consumer"].set()
        thread.join(10)


def test_genesis_preserves_original_lineage_and_opaque_lease_epoch(genesis) -> None:
    # A valid empty store must not gain a fabricated head, ancestry or lease.
    root, stage, writer = genesis
    lineage = writer.connection.execute(
        "SELECT value FROM store_meta WHERE key='lineage'"
    ).fetchone()[0]
    lineage = json.dumps(json.loads(lineage), indent=2)
    with writer.transaction() as conn:
        conn.execute("UPDATE store_meta SET value=? WHERE key='lineage'", (lineage,))
        conn.execute("INSERT INTO store_meta VALUES ('lease_epoch','opaque:epoch/7')")
    unrelated = stage / "caller.sqlite"
    unrelated.write_bytes(b"unrelated")
    with pytest.raises(RuntimeError, match="consumer failed"):
        with snapshot.staged_snapshot(
            root, directory=stage, deadline=time.monotonic() + 10
        ) as artifact:
            assert artifact.commit_seq == 0
            assert artifact.head_hash is None
            assert artifact.lineage_json == lineage
            assert artifact.lease_epoch == "opaque:epoch/7"
            if os.name == "posix":
                assert artifact.path.stat().st_mode & 0o777 == 0o600
            raise RuntimeError("consumer failed")
    assert list(stage.iterdir()) == [unrelated]
    assert unrelated.read_bytes() == b"unrelated"


@pytest.mark.parametrize(
    "key,value",
    [
        ("store_id", "not-a-uuid"),
        ("instance_id", "not-a-uuid"),
        ("commit_seq", "-1"),
        ("store_head_hash", "malformed"),
        ("store_head_hash", H),
        ("lineage", "not-json"),
        ("lineage", "[]"),
        (
            "lineage",
            '[{"instance_id":"not-a-uuid","adopted_from":null,"adopted_at_commit_seq":0,"head_hash":null}]',
        ),
        (
            "lineage",
            '[{"instance_id":"00000000-0000-4000-8000-000000000002","adopted_from":null,"adopted_at_commit_seq":0,"head_hash":null}]',
        ),
    ],
)
def test_sqlite_integrity_does_not_make_malformed_store_identity_valid(genesis, key, value) -> None:
    # SQLite accepts these metadata strings; restoration must reject them.
    root, stage, writer = genesis
    if key == "store_head_hash" and value == "malformed":
        _commit(writer)
    with writer.transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO store_meta VALUES (?,?)", (key, value))
    assert writer.connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    with pytest.raises(connection.CollectionStoreError, match="COLLECTION_SNAPSHOT_INVALID"):
        with snapshot.staged_snapshot(root, directory=stage, deadline=time.monotonic() + 10):
            pytest.fail("invalid identity was yielded")
    assert list(stage.iterdir()) == []


def test_metadata_head_must_match_copied_transaction_tail(genesis) -> None:
    # An individually valid hash can still falsely claim a different commit.
    root, stage, writer = genesis
    _commit(writer, initial=True)
    with writer.transaction() as conn:
        conn.execute("UPDATE store_meta SET value=? WHERE key='store_head_hash'", ("b" * 64,))
    with pytest.raises(connection.CollectionStoreError, match="COLLECTION_SNAPSHOT_INVALID"):
        with snapshot.staged_snapshot(root, directory=stage, deadline=time.monotonic() + 10):
            pytest.fail("mismatching tail was yielded")
    assert list(stage.iterdir()) == []


def test_foreign_key_failure_yields_no_artifact(genesis) -> None:
    # A valid SQLite database may still contain broken canonical row references.
    root, stage, writer = genesis
    writer.connection.execute("PRAGMA foreign_keys=OFF")
    with writer.transaction() as conn:
        conn.execute(
            "INSERT INTO audit_effects(txn_id,ordinal,item_key,effect) VALUES (7,0,'k','insert')"
        )
    with pytest.raises(connection.CollectionStoreError, match="COLLECTION_SNAPSHOT_INVALID"):
        with snapshot.staged_snapshot(root, directory=stage, deadline=time.monotonic() + 10):
            pytest.fail("broken foreign key was yielded")
    assert list(stage.iterdir()) == []


def test_fsync_failure_yields_nothing_and_cleans_only_owned_files(genesis, monkeypatch) -> None:
    # A completed copy is not a durable artifact until its file fsync succeeds.
    root, stage, _writer = genesis
    unrelated = stage / "keep.txt"
    unrelated.write_text("keep")

    def fail_fsync(_fd):
        raise OSError("file fsync failed")

    monkeypatch.setattr(os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="file fsync failed"):
        with snapshot.staged_snapshot(root, directory=stage, deadline=time.monotonic() + 10):
            pytest.fail("unflushed artifact was yielded")
    assert list(stage.iterdir()) == [unrelated]


def test_repeated_backup_busy_stops_at_deadline(genesis, monkeypatch) -> None:
    # SQLite backup retries BUSY indefinitely unless its progress callback aborts.
    root, stage, writer = genesis
    busy = []
    monotonic = time.monotonic

    class BusyReader(sqlite3.Connection):
        def backup(self, target, **kwargs):
            progress = kwargs["progress"]
            path = next(stage.iterdir())
            with closing(sqlite3.connect(path, isolation_level=None)) as locker:
                locker.execute("CREATE TABLE lock_probe(value)")
                locker.execute("BEGIN EXCLUSIVE")

                def observe(status, remaining, total):
                    if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                        busy.append(status)
                    progress(status, remaining, total)
                    if len(busy) > 3:
                        raise AssertionError("backup ignored deadline")

                try:
                    super().backup(target, **{**kwargs, "progress": observe})
                finally:
                    locker.execute("ROLLBACK")

    deadline = monotonic() + 30
    monkeypatch.setattr(connection, "_CONNECTION_FACTORY", BusyReader)
    monkeypatch.setattr(time, "monotonic", lambda: deadline + 1 if len(busy) >= 2 else deadline - 1)
    with pytest.raises(TimeoutError, match="COLLECTION_SNAPSHOT_DEADLINE"):
        with snapshot.staged_snapshot(root, directory=stage, deadline=deadline):
            pytest.fail("busy copy was yielded")
    assert len(busy) == 2
    assert list(stage.iterdir()) == []
    assert writer.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)


def test_copy_cancellation_releases_pinned_source_and_owned_scratch(genesis, monkeypatch) -> None:
    # Aborting a partial copy must not leave a reader pinning the live WAL.
    root, stage, writer = genesis
    _commit(writer, initial=True)
    cancel = threading.Event()

    class CancelReader(sqlite3.Connection):
        def backup(self, target, **kwargs):
            progress = kwargs["progress"]

            def observe(status, remaining, total):
                if status == sqlite3.SQLITE_OK and remaining:
                    cancel.set()
                progress(status, remaining, total)

            super().backup(target, **{**kwargs, "progress": observe})

    monkeypatch.setattr(connection, "_CONNECTION_FACTORY", CancelReader)
    with pytest.raises(connection.CollectionStoreError, match="COLLECTION_SNAPSHOT_CANCELLED"):
        with snapshot.staged_snapshot(
            root, directory=stage, deadline=time.monotonic() + 10, cancelled=cancel.is_set
        ):
            pytest.fail("cancelled copy was yielded")
    assert cancel.is_set()
    assert list(stage.iterdir()) == []
    _commit(writer)
    assert writer.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)


def test_validation_cancellation_is_reported_as_cancellation(genesis, monkeypatch) -> None:
    # SQL progress-handler interruption must retain its cancellation cause.
    root, stage, writer = genesis
    _commit(writer, initial=True)
    validating = begun = False
    interrupted = []
    original_connect = sqlite3.connect
    monotonic = time.monotonic

    class ValidationConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            nonlocal validating, begun
            begun |= sql == "PRAGMA journal_mode=DELETE"  # the copy's validation has begun
            if sql == "PRAGMA integrity_check":
                validating = True
            try:
                return super().execute(sql, *args, **kwargs)
            except sqlite3.OperationalError:
                interrupted.append(sql)
                raise

    def connect(*args, **kwargs):
        if not kwargs.get("uri"):
            kwargs["factory"] = ValidationConnection
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    # The integrity check outlasts one recheck interval, so the handler consults ``cancelled``.
    monkeypatch.setattr(
        time, "monotonic", lambda: monotonic() + (snapshot._RECHECK_SECONDS if validating else 0)
    )
    with pytest.raises(connection.CollectionStoreError, match="COLLECTION_SNAPSHOT_CANCELLED"):
        with snapshot.staged_snapshot(
            root, directory=stage, deadline=time.monotonic() + 10, cancelled=lambda: validating
        ):
            pytest.fail("cancelled validation was yielded")
    assert interrupted == ["PRAGMA integrity_check"]
    assert list(stage.iterdir()) == []
    assert writer.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)
    # A transient error from the caller's own check at a validation phase boundary, such as a
    # locked store read, stays that error: it says nothing about the copy.
    begun, locked = False, sqlite3.OperationalError("database is locked")

    def transient():
        if begun:
            raise locked
        return False

    with pytest.raises(sqlite3.OperationalError) as raised:
        with snapshot.staged_snapshot(root, directory=stage, deadline=time.monotonic() + 10, cancelled=transient):
            pytest.fail("validation that could not check its caller was yielded")
    assert raised.value is locked


def test_reader_timeout_override_preserves_default(genesis) -> None:
    # A backup's short lock wait must not shorten ordinary read operations.
    _root, _stage, writer = genesis
    with closing(connection.open_reader(writer.path, busy_timeout_ms=17)) as reader:
        assert reader.execute("PRAGMA busy_timeout").fetchone() == (17,)
        assert reader.execute("PRAGMA query_only").fetchone() == (1,)
    with closing(connection.open_reader(writer.path)) as reader:
        assert reader.execute("PRAGMA busy_timeout").fetchone() == (connection.BUSY_TIMEOUT_MS,)


@pytest.mark.parametrize("suffix", ["", "-journal"])
def test_unique_scratch_never_overwrites_a_preexisting_family_member(genesis, monkeypatch, suffix):
    # Even a token collision must not adopt, overwrite or remove caller bytes.
    root, stage, _writer = genesis
    token = "f" * 32
    existing = stage / f".exomem-collection-snapshot-{token}.sqlite{suffix}"
    existing.write_bytes(b"caller bytes")
    monkeypatch.setattr(snapshot.secrets, "token_hex", lambda _count: token)
    with pytest.raises((held_fs.HeldFsError, connection.CollectionStoreError)):
        with snapshot.staged_snapshot(root, directory=stage, deadline=time.monotonic() + 10):
            pytest.fail("existing family member was adopted")
    assert list(stage.iterdir()) == [existing]
    assert existing.read_bytes() == b"caller bytes"


def test_recorded_scratch_token_names_one_family_and_rejects_path_input(genesis):
    root, stage, _writer = genesis
    token = "b" * 32
    with snapshot.staged_snapshot(
        root, directory=stage, deadline=time.monotonic() + 10, scratch_token=token
    ) as artifact:
        assert artifact.path.name == ".exomem-collection-snapshot-" + token + ".sqlite"
    with pytest.raises(ValueError, match="invalid snapshot scratch token"):
        with snapshot.staged_snapshot(
            root, directory=stage, deadline=time.monotonic() + 10, scratch_token="../owner"
        ):
            pytest.fail("invalid scratch token was accepted")
    assert not list(stage.iterdir())


def test_snapshot_family_is_reserved_at_any_target_depth(genesis) -> None:
    # Scratch and SQLite companions must never enter user-content reads.
    root, _stage, _writer = genesis
    stage = root / "Knowledge Base" / "Notes"
    stage.mkdir(parents=True, mode=0o700)
    with snapshot.staged_snapshot(
        root, directory=stage, deadline=time.monotonic() + 10
    ) as artifact:
        for suffix in ("", "-wal", "-shm", "-journal"):
            relative = artifact.path.relative_to(root).as_posix() + suffix
            classified = reserved_paths.classify_logical(relative)
            assert classified.descriptor_id == "collection-snapshot"
            with pytest.raises(reserved_paths.ReservedPathLeafError, match="RESERVED_PATH"):
                reserved_paths.read_generic_bytes(root, relative)
        descriptor = next(
            d for d in reserved_paths.internal_state_registry() if d.id == "collection-snapshot"
        )
        assert descriptor.placement is reserved_paths.StatePlacement.TARGET_ADJACENT


def test_context_cleanup_preserves_a_foreign_replacement(genesis) -> None:
    # A reused temp name does not grant deletion authority over the new file.
    root, stage, _writer = genesis
    with snapshot.staged_snapshot(
        root, directory=stage, deadline=time.monotonic() + 10
    ) as artifact:
        moved = stage / "consumer-copy.sqlite"
        artifact.path.rename(moved)
        artifact.path.write_bytes(b"foreign replacement")
        companion = artifact.path.with_name(artifact.path.name + "-journal")
        companion.write_bytes(b"foreign companion")
    assert artifact.path.read_bytes() == b"foreign replacement"
    assert companion.read_bytes() == b"foreign companion"
    assert moved.is_file()


def test_context_cleanup_removes_scratch_after_hard_link_install(genesis) -> None:
    # A successful no-clobber install must not leave another full backup behind.
    root, stage, _writer = genesis
    installed = stage / "installed.sqlite"
    with snapshot.staged_snapshot(
        root, directory=stage, deadline=time.monotonic() + 10
    ) as artifact:
        scratch = artifact.path
        os.link(scratch, installed)
        expected_hash = artifact.file_sha256
    assert not scratch.exists()
    assert hashlib.sha256(installed.read_bytes()).hexdigest() == expected_hash


@pytest.mark.skipif(os.name != "posix", reason="unlink/recreate requires POSIX deletion semantics")
def test_context_cleanup_preserves_replacement_after_scratch_is_unlinked(genesis) -> None:
    # The kernel may reuse a released inode; its number alone is not ownership.
    root, stage, _writer = genesis
    with snapshot.staged_snapshot(
        root, directory=stage, deadline=time.monotonic() + 10
    ) as artifact:
        artifact.path.unlink()
        artifact.path.write_bytes(b"unrelated replacement")
    assert artifact.path.read_bytes() == b"unrelated replacement"


def test_sqlite_integrity_failure_yields_no_artifact(genesis) -> None:
    # CHECK corruption is not detected by foreign-key or metadata validation.
    root, stage, writer = genesis
    _commit(writer, initial=True)
    writer.connection.execute("PRAGMA ignore_check_constraints=ON")
    with writer.transaction() as conn:
        conn.execute("UPDATE collections SET layout='invalid-layout'")
    writer.connection.execute("PRAGMA ignore_check_constraints=OFF")
    assert writer.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert writer.connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
    with pytest.raises(connection.CollectionStoreError, match="COLLECTION_SNAPSHOT_INVALID"):
        with snapshot.staged_snapshot(root, directory=stage, deadline=time.monotonic() + 10):
            pytest.fail("integrity failure was yielded")
    assert list(stage.iterdir()) == []


def test_snapshot_works_when_backend_refuses_elevated_root_directory(genesis, monkeypatch):
    # Windows permits leaf creation but rejects mutate access on the root itself.
    root, stage, _writer = genesis
    backend = held_fs._backend()
    cls = backend.WindowsHeldFilesystem if os.name == "nt" else backend.PosixHeldFilesystem
    original_parent = cls.parent

    def parent(self, relative, *, access="read", **kwargs):
        if relative == "." and access != "read":
            return held_fs.HeldResult(
                error=held_fs.HeldFsError("INVALID_INPUT", "elevated root access")
            )
        return original_parent(self, relative, access=access, **kwargs)

    monkeypatch.setattr(cls, "parent", parent)
    with snapshot.staged_snapshot(
        root, directory=stage, deadline=time.monotonic() + 10
    ) as artifact:
        assert artifact.path.is_file()
    assert list(stage.iterdir()) == []
