"""Operator exports preserve stopped canonical WAL commits before Cloud publication."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
import uuid
from contextlib import closing
from pathlib import Path

import pytest
from test_collection_store_s1_gate import (
    LATER,
    _earlier_c,
    coordinator,
)
from test_collection_store_s1_gate import (
    ab as ab,
)
from test_collection_store_s1_gate import (
    abc as abc,
)
from test_collection_store_s1_gate import (
    host as serving_host,
)
from test_collection_store_summary import summary_text
from test_collection_store_writer import CID, manifest_path

from exomem import cloud_import_job, state_migration, state_paths, vault_portability
from exomem import cloud_operator_portability as operator
from exomem.cloud_import import ArchiveRefused
from exomem.collection_store import (
    admission,
    authority,
    chain,
    connection,
    replica,
    schema,
    snapshot,
)


def _backup_roots(found, tmp_path):
    restored = tmp_path / "restored-backup"
    vault = shutil.copytree(found.root, restored / "vault")
    host = restored / "host"
    state = host / "reviewed-state"
    state.mkdir(parents=True)
    shutil.copyfile(state_paths.vault_state_dir(found.root) / state_migration.MANIFEST_NAME,
                    state / state_migration.MANIFEST_NAME)
    with snapshot.staged_store_snapshot(found.session.path, directory=state,
                                        deadline=time.monotonic() + 15, preserve_schema=True) as copied:
        shutil.copyfile(copied.path, state / connection.STORE_FILENAME)
    return vault, host, state


def _request():
    return cloud_import_job.CloudImportRequest(
        import_id=str(uuid.uuid4()), cell_id="abcdefghijklmnop", kind="restore",
        ciphertext_sha256="a" * 64, ciphertext_bytes=1, declared_bytes=0, declared_files=0,
    )


def _sentinel(root):
    path = root / "Knowledge Base/_Schema/SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Invented schema\n")


def test_offline_cli_tar_and_cloud_restore_keep_a_wal_only_row(abc, tmp_path, monkeypatch):
    """A lagging replica must not drop an acknowledged row committed only in stopped WAL bytes."""
    vault, host, state = _backup_roots(abc, tmp_path)
    _sentinel(vault)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(Path(__file__).resolve().parents[1] / "src"),
                                                      str(Path(__file__).resolve().parent)))}
    # Build a stopped fixture through the real writer, then exit without SQLite's final checkpoint.
    script = """
import os, sys
from pathlib import Path
import exomem
assert Path(exomem.__file__).resolve().parent == Path(sys.argv[3]).resolve()
from exomem.collection_store import connection
from exomem.collection_store.writer import CollectionWriter
from exomem.governance.principal import library_scope
handle = connection.open_writer(Path(sys.argv[1]), lease_check=lambda: True)
with library_scope():
    writer = CollectionWriter(Path(sys.argv[2]), handle)
    result = writer.append_record(sys.argv[4], item={"title":"WAL acknowledged"}, item_key=sys.argv[5], why="fixture")
    assert result["outcome"] == "committed"
os._exit(0)
"""
    package = Path(__file__).resolve().parents[1] / "src/exomem"
    subprocess.run([sys.executable, "-c", script, str(state / connection.STORE_FILENAME), str(vault),
                    str(package), CID, LATER], env=env, check=True, capture_output=True)
    assert (state / (connection.STORE_FILENAME + "-wal")).stat().st_size > 0
    frozen = tmp_path / "frozen"
    shutil.copytree(vault, frozen / "vault")
    shutil.copytree(host, frozen / "host")
    shutil.rmtree(vault.parent)  # The original backup roots are now unavailable.
    vault, host, state = frozen / "vault", frozen / "host", frozen / "host/reviewed-state"
    original_bytes = {path.name: path.read_bytes() for path in state.iterdir()}
    publication = replica.replica_path(vault).parent / (".exomem-collection-replica-work-" + "a" * 32)
    publication.mkdir()
    (publication / "uncommitted.md").write_text("private publication work")
    args = ["--vault", str(vault), "--host-root", str(host), "--original-vault", str(abc.root),
            "--source-state-directory", str(state), "--timeout-seconds", "20"]
    completed = subprocess.run([sys.executable, "-m", "exomem.cloud_operator_portability", *args],
                               env=env, check=True, capture_output=True, text=True)
    receipt = json.loads(completed.stdout)
    assert receipt["ok"] and receipt["store"] == "prepared"
    assert original_bytes == {path.name: path.read_bytes() for path in state.iterdir()}
    assert operator.prepare_export(vault, host, abc.root, source_state_directory=state,
                                   timeout_seconds=20)["head_hash"] == receipt["head_hash"]
    marker_bytes = authority.read_marker(vault)
    with closing(connection.open_reader(replica.replica_path(vault))) as reader:
        expected_meta = dict(reader.execute("SELECT key,value FROM store_meta"))
        expected_history = reader.execute("SELECT * FROM txns ORDER BY commit_seq").fetchall()
        assert chain.verify_store_chain(reader) == (receipt["commit_seq"], receipt["head_hash"])
        assert reader.execute("SELECT values_json FROM items WHERE item_key=?", (LATER,)).fetchone()
        assert reader.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    abandoned_snapshot = publication.parent / (snapshot._PREFIX + "b" * 32 + ".sqlite")
    abandoned_snapshot.write_bytes(b"interrupted snapshot")
    (abandoned_snapshot.with_name(abandoned_snapshot.name + "-wal")).write_bytes(b"interrupted WAL")
    (abandoned_snapshot.with_name(abandoned_snapshot.name + "-shm")).write_bytes(b"interrupted shared memory")
    tar = io.BytesIO()
    with tarfile.open(fileobj=tar, mode="w") as stream:
        for relative in operator.archive_members(vault):
            stream.add(vault / relative, arcname=(Path("vault") / relative).as_posix(), recursive=False)
    with tarfile.open(fileobj=io.BytesIO(tar.getvalue())) as stream:
        names = stream.getnames()
        assert not any("uncommitted" in name or snapshot._PREFIX in name
                       or publication.name in name or name.endswith(("-wal", "-shm")) or "/host/" in name
                       for name in names)
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "old.md").write_text("original")
    target_state = state_paths.ensure_vault_state_dir(destination)
    (target_state / "original-state").write_text("retained")
    before_proof = cloud_import_job._prove_restore_collections

    def prove_before_rename(root):
        assert (destination / "old.md").read_text() == "original"
        assert (target_state / "original-state").read_text() == "retained"
        before_proof(root)

    monkeypatch.setattr(cloud_import_job, "_prove_restore_collections", prove_before_rename)
    if shutil.which("age") and shutil.which("age-keygen"):
        monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
        monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", "abcdefghijklmnop")
        request = _request()
        recipient = cloud_import_job.prepare(destination.parent, request.import_id, request.cell_id)
        ciphertext = subprocess.run(["age", "-r", recipient], input=tar.getvalue(), capture_output=True, check=True).stdout
        files = [vault / relative for relative in operator.archive_members(vault) if (vault / relative).is_file()]
        request = request.model_copy(update={"ciphertext_bytes": len(ciphertext),
            "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
            "declared_files": len(files), "declared_bytes": sum(path.stat().st_size for path in files)})
        result = cloud_import_job.run_import(request, destination, lambda: io.BytesIO(ciphertext))
        assert result == {"ok": True, "files": len(files), "bytes": request.declared_bytes}
        assert cloud_import_job.run_import(request, destination, lambda: pytest.fail("exact replay fetched ciphertext")) == result
    else:
        from exomem.cloud_import import unpack_stream

        staging = tmp_path / "unpacked"
        unpack_stream(io.BytesIO(tar.getvalue()), staging, max_bytes=1024 * 1024 * 20)
        cloud_import_job._restore(destination, staging, _request())
    assert authority.read_marker(destination) == marker_bytes
    with closing(connection.open_reader(replica.replica_path(destination))) as reader:
        assert dict(reader.execute("SELECT key,value FROM store_meta")) == expected_meta
        assert reader.execute("SELECT * FROM txns ORDER BY commit_seq").fetchall() == expected_history
        assert reader.execute("SELECT values_json FROM items WHERE item_key=?", (LATER,)).fetchone()
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "admitted-state"))
    with coordinator(tmp_path / "admitted-coordinator.sqlite"), serving_host(destination, tmp_path, "restored") as fresh:
        assert fresh.open()["status"] == "admitted"
        assert fresh.read_c() == ["Canonical", "WAL acknowledged"]
    assert authority.read_marker(vault) == marker_bytes
    assert original_bytes == {path.name: path.read_bytes() for path in state.iterdir()}


@pytest.mark.parametrize("damage", ["binding", "chain", "ahead", "stale-target"])
def test_bad_source_refuses_without_replacing_replica(abc, tmp_path, damage):
    """Wrong original identity or corrupt history must not publish a plausible source database."""
    vault, host, state = _backup_roots(abc, tmp_path)
    if damage == "binding":
        path = state / state_migration.MANIFEST_NAME
        manifest = json.loads(path.read_bytes())
        manifest["vault_identity"] = "another-vault"
        path.write_text(json.dumps(manifest))
    elif damage == "chain":
        with sqlite3.connect(state / connection.STORE_FILENAME) as reader:
            for (name,) in reader.execute("SELECT name FROM sqlite_schema WHERE type='trigger' AND tbl_name='txns'").fetchall():
                reader.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
            reader.execute("UPDATE txns SET event_hash=? WHERE commit_seq=1", ("b" * 64,))
    elif damage == "ahead":
        assert abc.write_c(LATER, "ahead of selected source")["outcome"] == "committed"
        abc.release()
        replica.replica_path(vault).write_bytes(replica.replica_path(abc.root).read_bytes())
    else:
        target = authority.parse_marker(vault, authority.read_marker(vault))
        target["authority_epoch"] += 1
        intent = {"version": 1, "request_id": "fixture", "request_hash": "fixture", "txn_id": 1,
                  "collection_id": CID, "manifest_path": manifest_path(), "source_path": "Items",
                  "expected_marker": None, "target_marker": json.dumps(target)}
        with sqlite3.connect(state / connection.STORE_FILENAME) as writer:
            writer.execute("INSERT OR REPLACE INTO store_meta(key,value) VALUES (?,?)",
                           (authority.PENDING_CREATE, json.dumps(intent)))
    before = replica.replica_path(vault).read_bytes()
    source_before = {p.name: p.read_bytes() for p in state.iterdir()}
    with pytest.raises((ValueError, state_migration.StateMigrationManifestError, chain.StoreChainError,
                        connection.CollectionStoreError)):
        operator.prepare_export(vault, host, abc.root, source_state_directory=state, timeout_seconds=20)
    assert replica.replica_path(vault).read_bytes() == before
    assert source_before == {p.name: p.read_bytes() for p in state.iterdir()}


@pytest.mark.parametrize("damage", ["mixed", "file", "chain"])
def test_cloud_collection_refusal_preserves_destination(abc, tmp_path, damage):
    """Collection proof and wrapper validation finish before the restore moves either destination root."""
    staging = shutil.copytree(abc.root, tmp_path / "incoming")
    _sentinel(staging)
    if damage == "mixed":
        wrapper = tmp_path / "wrapped"
        wrapper.mkdir()
        shutil.move(staging, wrapper / "vault")
        (wrapper / "extra.md").write_text("mixed")
        staging = wrapper
    elif damage == "file":
        (staging / "Knowledge Base/Legacy/invalid/_collection.md").parent.mkdir(parents=True)
        (staging / "Knowledge Base/Legacy/invalid/_collection.md").write_text("invalid manifest")
    else:
        with sqlite3.connect(replica.replica_path(staging)) as reader:
            for (name,) in reader.execute("SELECT name FROM sqlite_schema WHERE type='trigger' AND tbl_name='txns'").fetchall():
                reader.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
            reader.execute("UPDATE txns SET event_hash=? WHERE commit_seq=1", ("b" * 64,))
    vault = tmp_path / "destination"
    vault.mkdir()
    (vault / "old.md").write_text("original vault")
    state = state_paths.ensure_vault_state_dir(vault)
    (state / "old-state").write_text("original state")
    before = {p.relative_to(vault): p.read_bytes() for p in vault.rglob("*") if p.is_file()}
    with pytest.raises(ArchiveRefused):
        cloud_import_job._restore(vault, staging, _request())
    assert before == {p.relative_to(vault): p.read_bytes() for p in vault.rglob("*") if p.is_file()}
    assert (state / "old-state").read_text() == "original state"
    assert not list(vault.parent.glob(".restore-prior-*"))


def test_old_schema_validation_preserves_archived_replica(ab, tmp_path):
    """Checking a supported earlier schema must migrate only the disposable checking copy."""
    _earlier_c(ab)
    before = replica.replica_path(ab.root).read_bytes()
    with vault_portability.checked_collection_replica(ab.root) as reader:
        assert schema.schema_version(reader) == schema.SCHEMA_VERSION
        chain.verify_store_chain(reader)
    assert replica.replica_path(ab.root).read_bytes() == before
    vault, host_root, state = _backup_roots(ab, tmp_path)
    source_before = {path.name: path.read_bytes() for path in state.iterdir()}
    assert operator.prepare_export(vault, host_root, ab.root, source_state_directory=state,
                                   timeout_seconds=20)["ok"]
    with closing(sqlite3.connect(replica.replica_path(vault))) as reader:
        assert schema.schema_version(reader) == 10
    assert source_before == {path.name: path.read_bytes() for path in state.iterdir()}


@pytest.mark.parametrize("marker_version", [1, 2])
def test_canonical_snapshot_can_extend_the_lagging_replicas_marker(abc, tmp_path, marker_version):
    """A published replica may predate a newly admitted collection without being a fork."""
    old = replica.replica_path(abc.root).read_bytes()
    second = "66666666-6666-4666-8666-666666666666"
    result = admission.create_new(
        abc.session, abc.manager, manifest_path().replace("Work", "Later"),
        summary_text().replace(CID, second), why="second fixture", request_id="second",
        scaffold=True, fence_client=abc.operator,
    )
    assert result["status"] == "marker_admitted"
    vault, host_root, state = _backup_roots(abc, tmp_path)
    replica.replica_path(vault).write_bytes(old)
    if marker_version == 1:
        marker = authority.parse_marker(vault, authority.read_marker(vault))
        marker["version"] = 1
        for entry in marker["collections"]:
            entry.pop("source_path")
            entry.pop("layout")
        authority.marker_path(vault).write_text(json.dumps(marker))
    assert operator.prepare_export(vault, host_root, abc.root, source_state_directory=state,
                                   timeout_seconds=20)["ok"]
    with vault_portability.checked_collection_replica(vault) as reader:
        assert {row[0] for row in reader.execute("SELECT collection_id FROM collections")} == {CID, second}


@pytest.mark.parametrize("condition", ["matching-create", "other-create", "export", "divergence"])
def test_shared_replica_validation_keeps_pure_adoptability_conditions(abc, tmp_path, condition):
    """Matching pending creation stays valid; unresolved departure, mismatch and divergence cannot be adopted."""
    vault, host_root, state = _backup_roots(abc, tmp_path)
    raw = authority.read_marker(vault).decode()
    if condition == "divergence":
        key, value = schema.META_REPLICA_DIVERGENCE, "unresolved"
    elif condition == "export":
        marker = authority.parse_marker(vault, raw.encode())
        key, value = authority.PENDING_CREATE, json.dumps({
            "version": 2, "kind": authority.EXPORT, "request_id": "fixture", "plan_id": "fixture",
            "expected_marker": raw, "why": "fixture", "exported_at": "20261018T000000Z",
            "collections": [{name: entry[name] for name in ("collection_id", "manifest_path", "source_path")}
                            for entry in marker["collections"]],
        })
    else:
        key, value = authority.PENDING_CREATE, json.dumps({
            "version": 1, "request_id": "fixture", "request_hash": "fixture", "txn_id": 1,
            "collection_id": CID, "manifest_path": manifest_path(), "source_path": "Items",
            "expected_marker": None, "target_marker": raw if condition == "matching-create" else "{}",
        })
    with sqlite3.connect(replica.replica_path(vault)) as writer:
        writer.execute("INSERT OR REPLACE INTO store_meta(key,value) VALUES (?,?)", (key, value))
    before = replica.replica_path(vault).read_bytes()
    if condition == "matching-create":
        with vault_portability.checked_collection_replica(vault) as reader:
            assert authority.pending_create(reader)["target_marker"] == raw
    else:
        with pytest.raises(connection.CollectionStoreError):
            with vault_portability.checked_collection_replica(vault):
                pytest.fail("unresolved replica became adoptable")
        if condition in {"export", "divergence"}:
            with pytest.raises(connection.CollectionStoreError):
                operator.prepare_export(vault, host_root, abc.root, source_state_directory=state,
                                        timeout_seconds=20)
    assert replica.replica_path(vault).read_bytes() == before


def test_restored_generation_two_keeps_fresh_coordinator_refusal(abc, tmp_path, monkeypatch):
    """Byte restoration cannot reset a generation-2 marker to manufacture destination authority."""
    target = shutil.copytree(abc.root, tmp_path / "generation-two")
    marker = authority.parse_marker(target, authority.read_marker(target))
    marker["collection_store_fence"]["generation"] = 2
    authority.marker_path(target).write_text(json.dumps(marker))
    before = authority.read_marker(target)
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "generation-two-state"))
    with coordinator(tmp_path / "fresh-coordinator.sqlite"), serving_host(target, tmp_path, "restored") as fresh:
        with pytest.raises(connection.CollectionStoreError, match="COLLECTION_STORE_MARKER_CONFLICT"):
            fresh.open()
    assert authority.read_marker(target) == before


def test_reviewed_account_home_maps_one_source_without_environment_remapping(abc, tmp_path, monkeypatch):
    """The reviewed runtime placement maps to the restored host, rather than the current vault path."""
    vault, host_root, state = _backup_roots(abc, tmp_path)
    relative = state_paths.vault_state_dir(abc.root).relative_to(tmp_path)
    bound = host_root / relative
    bound.parent.mkdir(parents=True)
    shutil.move(state, bound)
    monkeypatch.setattr(operator.cell_init, "account_home", lambda: tmp_path)
    environment = dict(os.environ)
    assert operator.prepare_export(vault, host_root, abc.root, timeout_seconds=20)["ok"]
    assert dict(os.environ) == environment


@pytest.mark.parametrize("arguments", [[], ["--timeout-seconds", "private-path-canary"]])
def test_operator_argument_errors_do_not_echo_private_values(arguments, capsys):
    """The standard argparse error path must not print backup paths supplied as bad arguments."""
    assert operator.main(arguments) == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {"ok": False, "error": "OPERATOR_EXPORT_REFUSED"}
    assert output.err == "" and "private-path-canary" not in output.out
