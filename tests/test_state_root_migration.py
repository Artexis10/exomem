"""Explicit offline state migration pins (relocate-machine-local-state, task 1.3).

The contract under test (spec `machine-local-state-placement`):

- the explicit offline command over a vault with in-vault state and no
  external root relocates every machine-local family and stamps a completion
  marker;
- a later explicit offline command resumes an interrupted migration
  idempotently, and no family's bytes are deleted before being moved and
  verified;
- both-present ambiguity refuses rather than guesses: doctor FAIL naming both
  paths and the explicit remediation, nothing silently preferred or deleted;
- a vault with neither builds fresh (adoption on a new machine).

The mover is deliberately a single copy-verify-delete pipeline (a held-handle
read of the vault side, a fsynced staged write on the state-root side, digest
verification gating the source unlink). LOCALAPPDATA and the vault sit on
different volumes on measured cells, where rename is not atomic — so the
cross-volume path is the only path, and every test here covers it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


def _reset_resolution_cache() -> None:
    from exomem import state_migration

    state_migration.reset_state_resolution_cache_for_tests()


def _offline_authority():
    from exomem import state_migration

    return state_migration.assert_offline_migration_authority(
        source="state-root migration test",
    )


def _migrate(vault: Path, *, adopt: str | None = None):
    from exomem import state_migration

    return state_migration.migrate_vault_state_offline(
        vault,
        authority=_offline_authority(),
        adopt=adopt,
    )


def _seed_state(vault: Path) -> dict[str, bytes]:
    """Create a vault carrying one member of each movable top-level family."""
    from exomem.kbdir import kb_dirname

    kb = vault / kb_dirname()
    kb.mkdir(parents=True, exist_ok=True)
    members = {
        ".graph-sync.json": b'{"epoch": "seed"}',
        ".graph-sync-floor.json": b'{"floor": "seed"}',
        ".graph-sync-recovery.json": b'{"recovery": "seed"}',
        ".deferred-index.sqlite": b"deferred-bytes",
        ".embeddings.sqlite": b"embedding-bytes",
        ".embeddings.sqlite-wal": b"embedding-wal-bytes",
        ".lexical.sqlite": b"lexical-bytes",
        ".graph.sqlite": b"graph-bytes",
        ".claims.sqlite": b"claims-bytes",
        ".references.sqlite": b"legacy-references-bytes",
        ".clip.sqlite": b"clip-bytes",
        ".refs.sqlite": b"refs-bytes",
        ".freshness.sqlite": b"legacy-freshness-bytes",
        ".media-jobs.sqlite": b"media-jobs-bytes",
        ".media-worker.lock": b"",
        ".idempotency.sqlite": b"legacy-idempotency-bytes",
        ".voice_profiles.json": b'{"speaker": {"centroid": [1.0]}}',
        ".governance.sqlite": b"governance-bytes",
        ".due-state.json": b'{"version": 0}',
        ".review-state.json": b'{"version": 0}',
        ".graph-commit-receipts/aaaaaaaaaaaaaaaaaaaaaaaa.json": b'{"receipt": 1}',
        ".graph-commit-receipts/bbbbbbbbbbbbbbbbbbbbbbbb.json": b'{"receipt": 2}',
        ".authorization-projections/namespace/rows.sqlite": b"projection-bytes",
        ".authorization-projections/namespace/measurements/vector/family/rows.sqlite": (
            b"measurement-bytes"
        ),
        ".graph-coordination/legacy.lock": b"legacy-coordination",
        ".graph-reset-aaaaaaaaaaaaaaaaaaaaaaaa/.manifest.json": b'{"phase": "isolated"}',
        ".lexical.sqlite.rebuild-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.tmp": b"lex-rebuild",
        ".lexical.sqlite-wal.quarantine-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": (
            b"lex-quarantine"
        ),
        ".graph-rebuild-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-aaaaaaaaaaaaaaaaaaaaaaaa.sqlite": (
            b"graph-rebuild"
        ),
    }
    for name, data in members.items():
        target = kb / Path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    # Vault content that must never move.
    (kb / "Notes").mkdir(exist_ok=True)
    (kb / "Notes" / "a-note.md").write_text("# stays in the vault\n", encoding="utf-8")
    (kb / "_access.yaml").write_text("tiers: {}\n", encoding="utf-8")
    return members


def test_rollback_session_binds_backup_reference_by_operation(tmp_path: Path) -> None:
    """A backup restore marker carries its immutable verified backup reference."""
    from exomem import state_migration

    vault = tmp_path / "vault"
    _seed_state(vault)
    _migrate(vault)
    reference = "exomem-governance-v3-backup://sha256/" + "a" * 64

    with state_migration.governance_rollback_session(vault) as session:
        marker = session.begin_prepared(
            operation="governance_schema_v3_backup_restore",
            event_id="e" * 64,
            plan_digest="b" * 64,
            target_digest="c" * 64,
            timestamp=1,
            d0="d" * 64,
            backup_reference=reference,
            backup_plan_digest="f" * 64,
            source_store_digest="1" * 64,
        )

    assert marker["backup_reference"] == reference


@pytest.mark.parametrize("field", ("timestamp", "seq", "byte_offset"))
def test_rollback_marker_rejects_boolean_numeric_fields(tmp_path: Path, field: str) -> None:
    from exomem import state_migration

    vault = tmp_path / "vault"
    manifest = state_migration._new_manifest(vault, state_migration._descriptor_ids())
    manifest["families"] = {
        descriptor: {"status": "complete"} for descriptor in manifest["descriptors"]
    }
    manifest["state"] = "complete"
    event_id = "e" * 64
    instance_id = "f" * 32
    marker = {
        "operation": "governance_schema_v4_downmigration",
        "event_id": event_id,
        "phase": "complete",
        "plan_digest": "a" * 64,
        "target_digest": "b" * 64,
        "timestamp": 1,
        "d0": "c" * 64,
        "legacy_path": "Knowledge Base/.governance.sqlite",
        "stage_leaf": f".governance-v3-rollback-{event_id}.sqlite",
        "backup_reference": None,
        "backup_plan_digest": None,
        "source_store_digest": None,
        "schema_fence_generation": None,
        "d1": "d" * 64,
        "terminal": {
            "instance_id": instance_id,
            "seq": 2,
            "hash": "1" * 64,
            "path": f"Knowledge Base/_Governance/events/{instance_id}/2026-08.jsonl",
            "byte_offset": 7,
        },
    }
    manifest["version"] = 2
    manifest["governance_rollback"] = marker
    if field == "timestamp":
        marker[field] = True
    else:
        marker["terminal"][field] = True

    with pytest.raises(state_migration.StateMigrationManifestError):
        state_migration._validate_manifest(tmp_path / "manifest.json", manifest, vault_root=vault)


def test_marker_free_v2_manifest_remains_ready_after_adoption(tmp_path: Path) -> None:
    from exomem import state_migration, state_paths

    vault = tmp_path / "vault"
    _seed_state(vault)
    _migrate(vault)
    manifest_path = state_paths.vault_state_dir(vault) / state_migration.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = 2
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _reset_resolution_cache()

    assert state_migration.require_vault_state_ready(vault).state_dir == state_paths.vault_state_dir(vault)


def test_legacy_adoption_requires_a_receipt_head_descending_from_d1(tmp_path: Path) -> None:
    """Descriptor adoption cannot accept an unanchored legacy receipt head."""
    from exomem.governance import receipts

    vault = tmp_path / "vault"
    instance_id = "1" * 32
    event_id = "2" * 64
    evidence = vault / "Knowledge Base" / "_Governance" / "events" / instance_id
    evidence.mkdir(parents=True)
    intent = {
        "schema": receipts.SCHEMA,
        "event_id": event_id,
        "event_type": "critical",
        "phase": "intent",
        "timestamp": "2026-08-01T00:00:00Z",
        "instance_id": instance_id,
        "seq": 1,
        "prev": receipts.GENESIS_HASH,
        "durable": True,
        "operation": "governance-schema-rollback",
        "prior": "3" * 64,
        "target": "4" * 64,
        "affected_ids": [],
    }
    intent["hash"] = receipts._record_hash(intent)  # noqa: SLF001 - frozen evidence fixture
    terminal = {
        "schema": receipts.SCHEMA,
        "event_id": f"{event_id}:committed",
        "event_type": "critical",
        "phase": "committed",
        "timestamp": "2026-08-01T00:00:01Z",
        "instance_id": instance_id,
        "seq": 2,
        "prev": intent["hash"],
        "durable": True,
        "causation_id": event_id,
        "outcome": "schema-v3-restored",
    }
    terminal["hash"] = receipts._record_hash(terminal)  # noqa: SLF001 - frozen evidence fixture
    month = evidence / "2026-08.jsonl"
    intent_line = json.dumps(intent, sort_keys=True, separators=(",", ":")) + "\n"
    terminal_line = json.dumps(terminal, sort_keys=True, separators=(",", ":")) + "\n"
    month.write_bytes((intent_line + terminal_line).encode("utf-8"))
    locator = f"Knowledge Base/_Governance/events/{instance_id}/2026-08.jsonl"
    legacy = vault / "Knowledge Base" / ".governance.sqlite"
    connection = sqlite3.connect(legacy)
    try:
        connection.execute("CREATE TABLE receipt_instance (singleton INTEGER, instance_id TEXT)")
        connection.execute(
            "CREATE TABLE receipts_head (instance_id TEXT, durable_seq INTEGER, durable_hash TEXT, "
            "observed_seq INTEGER, observed_hash TEXT, path TEXT, byte_offset INTEGER)"
        )
        connection.execute("INSERT INTO receipt_instance VALUES (1, ?)", (instance_id,))
        connection.execute(
            "INSERT INTO receipts_head VALUES (?, ?, ?, ?, ?, ?, ?)",
            (instance_id, 2, terminal["hash"], 2, terminal["hash"], locator, len(intent_line.encode("utf-8"))),
        )
        connection.commit()
    finally:
        connection.close()

    endpoint = {
        "instance_id": instance_id,
        "seq": 2,
        "hash": terminal["hash"],
        "path": locator,
        "byte_offset": len(intent_line.encode("utf-8")),
    }
    assert receipts.require_legacy_receipt_descendant(vault, legacy, endpoint)["hash"] == terminal["hash"]

    connection = sqlite3.connect(legacy)
    try:
        connection.execute("UPDATE receipts_head SET observed_hash=?", ("0" * 64,))
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(receipts.ReceiptError, match="verified durable tail"):
        receipts.require_legacy_receipt_descendant(vault, legacy, endpoint)


@pytest.mark.parametrize(
    ("phase", "external", "legacy", "legacy_residue", "expected"),
    (
        ("prepared", "d1", "a", False, "copy"),
        ("prepared", "a", "a", False, "mark-copied"),
        ("copied", "a", "a", False, "remove"),
        ("copied", "a", None, False, "clear"),
        # Crash after unlinking the main database but before deleting its
        # SQLite sidecars resumes only that bounded residue cleanup.
        ("copied", "a", None, True, "remove-residue"),
    ),
)
def test_governance_adoption_replays_each_durable_crash_cut(
    phase: str,
    external: str,
    legacy: str | None,
    legacy_residue: bool,
    expected: str,
) -> None:
    from exomem import state_migration

    assert (
        state_migration._governance_adoption_replay_action(  # noqa: SLF001 - crash matrix seam
            phase=phase,
            external_digest=external,
            legacy_digest=legacy,
            legacy_residue=legacy_residue,
            d1_digest="d1",
            adopted_digest="a",
        )
        == expected
    )


def test_governance_adoption_replay_refuses_mixed_authorities() -> None:
    from exomem import state_migration

    with pytest.raises(state_migration.StateMigrationOfflineRequired):
        state_migration._governance_adoption_replay_action(  # noqa: SLF001 - crash matrix seam
            phase="prepared",
            external_digest="d1",
            legacy_digest=None,
            d1_digest="d1",
            adopted_digest="a",
        )


def test_governance_adoption_recovers_only_sqlite_residue_after_main_unlink(tmp_path: Path) -> None:
    """The copied-marker replay owns sidecars left by its own main unlink cut."""
    from exomem import state_migration

    vault = tmp_path / "vault"
    kb = vault / "Knowledge Base"
    kb.mkdir(parents=True)
    for leaf in (
        ".governance.sqlite-wal",
        ".governance.sqlite-shm",
        ".governance.sqlite-journal",
    ):
        (kb / leaf).write_bytes(b"crash residue")

    state_migration._remove_legacy_governance_residue(vault)  # noqa: SLF001 - crash-cut seam

    assert not any(kb.glob(".governance.sqlite*"))


def test_legacy_adoption_refuses_main_file_snapshot_with_sqlite_sidecars(tmp_path: Path) -> None:
    """A raw held main-file read never silently omits committed WAL frames."""
    from exomem import state_migration

    vault = tmp_path / "vault"
    kb = vault / "Knowledge Base"
    kb.mkdir(parents=True)
    connection = sqlite3.connect(kb / ".governance.sqlite")
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE example (value TEXT)")
        connection.execute("INSERT INTO example VALUES ('committed')")
        connection.commit()
        assert (kb / ".governance.sqlite-wal").exists()
        with pytest.raises(state_migration.StateMigrationOfflineRequired):
            with state_migration._retained_legacy_governance_file(vault):  # noqa: SLF001
                pass
    finally:
        connection.close()


def _install_complete_rollback_adoption_fixture(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, str, bytes]:
    """Install actual exact-v3 D1/A authorities and their receipt anchor."""
    from exomem import sidecar_store, state_migration, state_paths
    from exomem.governance import receipts, store

    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(vault.parent / "machine-state"))
    state_dir = state_paths.ensure_vault_state_dir(vault)
    external = store.sidecar_path(vault)
    external.parent.mkdir(parents=True, exist_ok=True)
    instance_id = "1" * 32
    event_id = "2" * 64
    receipts_dir = vault / "Knowledge Base" / "_Governance" / "events" / instance_id
    receipts_dir.mkdir(parents=True)
    intent = {
        "schema": receipts.SCHEMA,
        "event_id": event_id,
        "event_type": "critical",
        "phase": "intent",
        "timestamp": "2026-08-01T00:00:00Z",
        "instance_id": instance_id,
        "seq": 1,
        "prev": receipts.GENESIS_HASH,
        "durable": True,
        "operation": "governance-schema-rollback",
        "prior": "3" * 64,
        "target": "4" * 64,
        "affected_ids": [],
    }
    intent["hash"] = receipts._record_hash(intent)  # noqa: SLF001 - immutable fixture record
    terminal = {
        "schema": receipts.SCHEMA,
        "event_id": f"{event_id}:committed",
        "event_type": "critical",
        "phase": "committed",
        "timestamp": "2026-08-01T00:00:01Z",
        "instance_id": instance_id,
        "seq": 2,
        "prev": intent["hash"],
        "durable": True,
        "causation_id": event_id,
        "outcome": "schema-v3-restored",
    }
    terminal["hash"] = receipts._record_hash(terminal)  # noqa: SLF001 - immutable fixture record
    intent_line = json.dumps(intent, sort_keys=True, separators=(",", ":")) + "\n"
    (receipts_dir / "2026-08.jsonl").write_bytes(
        (
            intent_line
            + json.dumps(terminal, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
    )
    endpoint = {
        "instance_id": instance_id,
        "seq": 2,
        "hash": terminal["hash"],
        "path": f"Knowledge Base/_Governance/events/{instance_id}/2026-08.jsonl",
        "byte_offset": len(intent_line.encode("utf-8")),
    }
    connection = sqlite3.connect(external)
    try:
        store._migrate(connection)  # noqa: SLF001 - exact-v3 fixture seam
        sidecar_store.ensure_meta_table(connection, store.DATA_TABLE, "adoption-fixture")
        connection.execute("INSERT INTO receipt_instance VALUES (1, ?)", (instance_id,))
        connection.execute(
            "INSERT INTO receipts_head VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                instance_id,
                2,
                terminal["hash"],
                2,
                terminal["hash"],
                endpoint["path"],
                endpoint["byte_offset"],
            ),
        )
        connection.commit()
        d1 = store._v3_snapshot_digest(connection)  # noqa: SLF001 - canonical D1 fixture proof
    finally:
        connection.close()
    legacy = vault / "Knowledge Base" / ".governance.sqlite"
    shutil.copy2(external, legacy)
    connection = sqlite3.connect(legacy)
    try:
        connection.execute("UPDATE meta SET value=314159 WHERE key='instance'")
        connection.commit()
    finally:
        connection.close()

    manifest = state_migration._new_manifest(vault, state_migration._descriptor_ids())
    manifest["version"] = 2
    manifest["state"] = "complete"
    manifest["families"] = {
        descriptor: {"status": "complete"} for descriptor in manifest["descriptors"]
    }
    manifest["governance_rollback"] = {
        "operation": "governance_schema_v4_downmigration",
        "event_id": event_id,
        "phase": "complete",
        "plan_digest": "a" * 64,
        "target_digest": "b" * 64,
        "timestamp": 1,
        "d0": "c" * 64,
        "legacy_path": "Knowledge Base/.governance.sqlite",
        "stage_leaf": f".governance-v3-rollback-{event_id}.sqlite",
        "backup_reference": None,
        "backup_plan_digest": None,
        "source_store_digest": None,
        "schema_fence_generation": None,
        "d1": d1,
        "terminal": endpoint,
    }
    state_migration._write_manifest(state_dir, manifest)  # noqa: SLF001 - durable fixture marker
    unrelated = state_dir / ".embeddings.sqlite"
    unrelated_bytes = b"unrelated-external-family"
    unrelated.write_bytes(unrelated_bytes)
    return state_dir, d1, unrelated_bytes


@pytest.mark.parametrize(
    "cut", ("after_external_copy", "after_copied_marker", "after_legacy_removal")
)
def test_governance_store_vault_adoption_replays_real_durable_cuts_without_touching_other_families(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cut: str,
) -> None:
    """Each adoption crash cut resumes against real v3 stores and receipt evidence."""
    from exomem import state_migration

    vault = tmp_path / "vault"
    vault.mkdir()
    state_dir, _d1, unrelated_bytes = _install_complete_rollback_adoption_fixture(
        vault, monkeypatch
    )

    def crash(point: str) -> None:
        if point == cut:
            raise RuntimeError(point)

    monkeypatch.setattr(state_migration, "_governance_adoption_barrier", crash)
    with pytest.raises(RuntimeError, match=cut):
        state_migration._adopt_governance_store_from_vault_offline(vault)  # noqa: SLF001

    monkeypatch.setattr(
        state_migration, "_governance_adoption_barrier", lambda _point: None
    )
    state_migration._adopt_governance_store_from_vault_offline(vault)  # noqa: SLF001

    assert not (vault / "Knowledge Base" / ".governance.sqlite").exists()
    assert (state_dir / ".embeddings.sqlite").read_bytes() == unrelated_bytes
    completed = json.loads((state_dir / state_migration.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert completed["version"] == 2
    assert "governance_rollback" not in completed
    assert "governance_adoption" not in completed


def test_governance_store_vault_adoption_refuses_external_change_before_legacy_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final A proof is inside identity coordination and gates legacy unlink."""
    from exomem import state_migration
    from exomem.governance import store

    vault = tmp_path / "vault"
    vault.mkdir()
    _state_dir, _d1, _unrelated = _install_complete_rollback_adoption_fixture(
        vault, monkeypatch
    )
    external = store.sidecar_path(vault)
    real_digest = state_migration._exact_v3_digest  # noqa: SLF001 - precise destructive-cut injection
    external_checks = 0

    def mutate_before_unlink(path: Path, *, detail: str) -> str:
        nonlocal external_checks
        if Path(path) == external:
            external_checks += 1
            # Calls one/two classify D1, three proves the copied A, and four
            # is the immediate pre-unlink proof guarded by the held identity.
            if external_checks == 4:
                connection = sqlite3.connect(external)
                try:
                    connection.execute("UPDATE meta SET value=271828 WHERE key='instance'")
                    connection.commit()
                finally:
                    connection.close()
        return real_digest(path, detail=detail)

    monkeypatch.setattr(state_migration, "_exact_v3_digest", mutate_before_unlink)
    with pytest.raises(state_migration.StateMigrationOfflineRequired):
        state_migration._adopt_governance_store_from_vault_offline(vault)  # noqa: SLF001

    assert external_checks == 4
    assert (vault / "Knowledge Base" / ".governance.sqlite").is_file()


def _kb_state_names(vault: Path) -> set[str]:
    from exomem.kbdir import kb_dirname

    kb = vault / kb_dirname()
    if not kb.is_dir():
        return set()
    return {entry.name for entry in kb.iterdir() if entry.name.startswith(".")}


def test_unreadable_knowledge_base_scan_fails_before_manifest_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import state_migration, state_paths
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    kb = vault / kb_dirname()
    kb.mkdir(parents=True)
    real_scandir = state_migration.os.scandir

    def refuse_knowledge_base(path: Path):
        if Path(path) == kb:
            raise PermissionError("injected unreadable knowledge base")
        return real_scandir(path)

    monkeypatch.setattr(state_migration.os, "scandir", refuse_knowledge_base)
    _reset_resolution_cache()

    with pytest.raises(PermissionError, match="unreadable knowledge base"):
        _migrate(vault)

    assert not (
        state_paths.vault_state_dir(vault) / state_migration.MANIFEST_NAME
    ).exists()


def test_doctor_fails_explicitly_when_knowledge_base_scan_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import doctor, state_migration, state_paths
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    kb = vault / kb_dirname()
    kb.mkdir(parents=True)
    monkeypatch.setattr(
        state_migration,
        "scan_vault_state",
        lambda _root: (_ for _ in ()).throw(PermissionError("private scan detail")),
    )

    check = doctor._check_state_placement(vault)

    assert check.status == "fail"
    assert "could not be inspected" in check.message
    assert "private scan detail" not in check.message
    assert check.details is not None
    assert check.details["state_root"] == str(state_paths.vault_state_dir(vault))
    assert check.details["in_vault_state_root"] == str(kb)
    assert check.details["in_vault_scan"] == "unavailable"


def test_ready_gate_refuses_an_active_legacy_wal_writer_without_mutation(
    tmp_path: Path,
) -> None:
    from exomem import state_migration, state_paths
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    database = vault / kb_dirname() / ".claims.sqlite"
    database.parent.mkdir(parents=True)
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("CREATE TABLE active_owner(value TEXT NOT NULL)")
        writer.execute("INSERT INTO active_owner VALUES ('preserved')")
        writer.commit()
        state_dir = state_paths.vault_state_dir(vault)
        assert not state_dir.exists()

        with pytest.raises(state_migration.StateMigrationOfflineRequired) as ready:
            state_migration.require_vault_state_ready(vault)
        assert ready.value.code == "STATE_MIGRATION_OFFLINE_REQUIRED"
        assert not state_dir.exists(), "ordinary readiness created external state"
        assert writer.execute("SELECT value FROM active_owner").fetchone() == (
            "preserved",
        )

        with pytest.raises(state_migration.StateMigrationOfflineRequired):
            state_migration.migrate_vault_state_offline(
                vault,
                authority=object(),
            )
        assert not state_dir.exists(), "unauthorized migration created external state"
        assert database.is_file()
    finally:
        writer.close()

    migrated = _migrate(vault)
    assert migrated.state_dir == state_dir
    assert not database.exists()
    _reset_resolution_cache()
    assert state_migration.require_vault_state_ready(vault).state_dir == state_dir


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX child-process locking")
def test_ready_gate_refuses_a_legacy_subprocess_wal_writer_then_preserves_commit(
    tmp_path: Path,
) -> None:
    """An old writer ignores the new lock and commits after startup refuses."""

    from exomem import state_migration, state_paths
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    database = vault / kb_dirname() / ".claims.sqlite"
    database.parent.mkdir(parents=True)
    child_program = r"""
import sqlite3
import sys

database = sys.argv[1]
writer = sqlite3.connect(database)
assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
writer.execute("CREATE TABLE legacy_owner(value TEXT NOT NULL)")
writer.execute("INSERT INTO legacy_owner VALUES ('before-refusal')")
writer.commit()
print("READY", flush=True)
if sys.stdin.readline().strip() != "commit":
    raise SystemExit("parent did not authorize the post-refusal commit")
writer.execute("INSERT INTO legacy_owner VALUES ('after-refusal')")
writer.commit()
print("COMMITTED", flush=True)
writer.close()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", child_program, str(database)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    try:
        assert process.stdout.readline().strip() == "READY"
        state_dir = state_paths.vault_state_dir(vault)
        assert not state_dir.exists()

        with pytest.raises(state_migration.StateMigrationOfflineRequired) as ready:
            state_migration.require_vault_state_ready(vault)
        assert ready.value.code == "STATE_MIGRATION_OFFLINE_REQUIRED"
        assert not state_dir.exists(), "ordinary startup copied legacy WAL state"
        assert database.is_file(), "ordinary startup unlinked the active source"

        process.stdin.write("commit\n")
        process.stdin.flush()
        assert process.stdout.readline().strip() == "COMMITTED"
        _, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)

    with sqlite3.connect(database) as reader:
        assert reader.execute(
            "SELECT value FROM legacy_owner ORDER BY rowid"
        ).fetchall() == [("before-refusal",), ("after-refusal",)]

    migrated = _migrate(vault)
    assert migrated.state_dir == state_dir
    assert not database.exists()
    with sqlite3.connect(state_dir / ".claims.sqlite") as reader:
        assert reader.execute(
            "SELECT value FROM legacy_owner ORDER BY rowid"
        ).fetchall() == [("before-refusal",), ("after-refusal",)]


def test_ready_gate_refuses_a_stale_complete_manifest_without_resuming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import doctor, state_migration, state_paths

    vault = tmp_path / "vault"
    (vault / "Knowledge Base").mkdir(parents=True)
    state_dir = state_paths.ensure_vault_state_dir(vault)
    descriptor_ids = state_migration._descriptor_ids()
    stale_ids = descriptor_ids[:-1]
    manifest = state_migration._new_manifest(vault, stale_ids)
    manifest["families"] = {
        descriptor_id: {"status": "complete"} for descriptor_id in stale_ids
    }
    manifest["state"] = "complete"
    state_migration._write_manifest(state_dir, manifest)
    before = (state_dir / state_migration.MANIFEST_NAME).read_bytes()
    _reset_resolution_cache()

    monkeypatch.setattr(
        state_paths,
        "ensure_vault_state_dir",
        lambda _root: pytest.fail("readiness created a state directory"),
    )
    monkeypatch.setattr(
        state_migration,
        "_write_manifest",
        lambda *_args: pytest.fail("readiness rewrote a stale manifest"),
    )
    monkeypatch.setattr(
        state_migration,
        "_move_family",
        lambda *_args: pytest.fail("readiness resumed migration"),
    )

    with pytest.raises(state_migration.StateMigrationOfflineRequired) as error:
        state_migration.require_vault_state_ready(vault)

    assert error.value.code == "STATE_MIGRATION_OFFLINE_REQUIRED"
    assert (state_dir / state_migration.MANIFEST_NAME).read_bytes() == before
    assert state_migration.migration_status(vault) == "stale"
    check = doctor._check_state_placement(vault)
    assert check.status == "fail"
    assert "--migrate-state --offline" in (check.remediation or "")


@pytest.mark.parametrize("manifest_state", ("absent", "in-progress"))
def test_doctor_refuses_state_that_ordinary_startup_cannot_admit(
    tmp_path: Path,
    manifest_state: str,
) -> None:
    from exomem import doctor, state_migration, state_paths

    vault = tmp_path / "vault"
    (vault / "Knowledge Base/Notes").mkdir(parents=True)
    if manifest_state == "in-progress":
        state_dir = state_paths.ensure_vault_state_dir(vault)
        state_migration._write_manifest(
            state_dir,
            state_migration._new_manifest(vault, state_migration._descriptor_ids()),
        )
    _reset_resolution_cache()

    check = doctor._check_state_placement(vault)

    assert check.status == "fail"
    assert "migration" in check.message.lower()
    assert "--migrate-state --offline" in (check.remediation or "")


def test_explicit_offline_migration_over_an_existing_vault_migrates(
    tmp_path: Path,
) -> None:
    from exomem import state_migration, state_paths
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    members = _seed_state(vault)
    _reset_resolution_cache()

    resolution = _migrate(vault)

    state_dir = state_paths.vault_state_dir(vault)
    assert resolution.state_dir == state_dir
    assert resolution.migrated is True
    assert state_migration.migration_completed(vault) is True
    for name, data in members.items():
        moved = state_dir / Path(name)
        assert moved.is_file(), f"{name} was not moved to the external root"
        assert moved.read_bytes() == data, f"{name} lost bytes in the move"
        assert not (vault / kb_dirname() / Path(name)).exists(), (
            f"{name} still exists in the vault after migration"
        )
    # Content stayed.
    assert (vault / kb_dirname() / "Notes" / "a-note.md").is_file()
    assert (vault / kb_dirname() / "_access.yaml").is_file()
    assert _kb_state_names(vault) == set(), (
        f"machine-local leftovers under the vault: {_kb_state_names(vault)}"
    )
    manifest = json.loads(
        (state_dir / state_migration.MANIFEST_NAME).read_text(encoding="utf-8")
    )
    assert manifest["version"] == state_migration.MANIFEST_VERSION
    assert manifest["vault_identity"] == state_paths.vault_state_key(vault)
    assert manifest["state"] == "complete"
    assert manifest["descriptors"] == sorted(
        descriptor.id
        for descriptor in __import__(
            "exomem.reserved_paths", fromlist=["external_state_descriptors"]
        ).external_state_descriptors()
    )
    for descriptor_id in (
        "authorization-projections",
        "claims-store",
        "graph-coordination",
        "graph-reset",
        "idempotency-store",
        "references-store",
        "voice-profile-store",
    ):
        assert manifest["families"][descriptor_id]["status"] == "complete"


def test_an_interrupted_migration_resumes_without_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import state_migration, state_paths
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    members = _seed_state(vault)
    _reset_resolution_cache()

    real_move = state_migration._move_family
    moved_families: list[str] = []

    def interrupt_after_first(*args, **kwargs):
        if moved_families:
            raise OSError("simulated crash mid-migration")
        result = real_move(*args, **kwargs)
        moved_families.append(str(args[2]))
        return result

    monkeypatch.setattr(state_migration, "_move_family", interrupt_after_first)
    with pytest.raises(OSError, match="simulated crash"):
        _migrate(vault)

    # Nothing lost mid-flight: every seeded member exists in exactly one of
    # the two places, with its exact bytes.
    state_dir = state_paths.vault_state_dir(vault)
    for name, data in members.items():
        in_vault = vault / kb_dirname() / Path(name)
        external = state_dir / Path(name)
        locations = [path for path in (in_vault, external) if path.is_file()]
        assert locations, f"{name} vanished during the interrupted migration"
        assert locations[0].read_bytes() == data, f"{name} lost bytes mid-flight"
    assert not state_migration.migration_completed(vault)

    # A later explicit offline run resumes the remaining families and completes.
    monkeypatch.setattr(state_migration, "_move_family", real_move)
    _reset_resolution_cache()
    resolution = _migrate(vault)
    assert resolution.migrated is True
    assert state_migration.migration_completed(vault) is True
    for name, data in members.items():
        assert (state_dir / Path(name)).read_bytes() == data
        assert not (vault / kb_dirname() / Path(name)).exists()


def test_migration_never_deletes_bytes_it_did_not_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy-verify-delete pin: a failed verification keeps the source."""
    from exomem import state_migration
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    members = _seed_state(vault)
    _reset_resolution_cache()

    verified: list[Path] = []

    def corrupted_digest(path: Path) -> str:
        verified.append(Path(path))
        return hashlib.sha256(b"not the source bytes").hexdigest()

    monkeypatch.setattr(state_migration, "_destination_digest", corrupted_digest)
    with pytest.raises(OSError, match="verification failed"):
        _migrate(vault)

    assert verified, "the mover never consulted destination verification"
    for name, data in members.items():
        source = vault / kb_dirname() / Path(name)
        assert source.is_file(), (
            f"{name} was deleted although its copy never verified"
        )
        assert source.read_bytes() == data
    assert not state_migration.migration_completed(vault)


def test_every_moved_file_passes_through_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-vacuity for the verify gate: one verification per moved file."""
    from exomem import state_migration

    vault = tmp_path / "vault"
    members = _seed_state(vault)
    _reset_resolution_cache()

    verified: list[Path] = []
    real_digest = state_migration._destination_digest

    def counting_digest(path: Path) -> str:
        verified.append(Path(path))
        return real_digest(path)

    monkeypatch.setattr(state_migration, "_destination_digest", counting_digest)
    resolution = _migrate(vault)

    assert resolution.migrated is True
    assert len(verified) >= len(members), (
        f"moved {len(members)} files but verified only {len(verified)}"
    )


def test_complete_manifest_with_legacy_duplicate_refuses_until_offline_adoption(
    tmp_path: Path,
) -> None:
    from exomem import doctor, state_migration, state_paths
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    _seed_state(vault)
    _reset_resolution_cache()
    _migrate(vault)

    # An older exomem (or a restored backup) reintroduces in-vault state.
    stray = vault / kb_dirname() / ".graph-sync.json"
    stray.write_bytes(b'{"epoch": "foreign"}')
    _reset_resolution_cache()

    with pytest.raises(state_migration.StateMigrationOfflineRequired) as ready:
        state_migration.require_vault_state_ready(vault)
    assert ready.value.code == "STATE_MIGRATION_OFFLINE_REQUIRED"
    with pytest.raises(state_migration.StatePlacementConflict):
        _migrate(vault)
    # Neither copy is deleted.
    assert stray.is_file()
    assert (state_paths.vault_state_dir(vault) / ".graph-sync.json").is_file()

    check = doctor._check_state_placement(vault)
    assert check.status == "fail"
    assert check.details is not None
    assert check.details["state_root"] == str(state_paths.vault_state_dir(vault))
    assert str(stray) in check.details["in_vault_leftovers"], (
        "the doctor FAIL must name the in-vault copy"
    )
    assert "--migrate-state --offline --adopt-state" in (check.remediation or "")


def test_dual_state_without_marker_refuses_to_guess(tmp_path: Path) -> None:
    import os

    from exomem import state_migration, state_paths
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    members = _seed_state(vault)
    # A non-empty external root that no completed migration produced (for
    # example a fresh-built cell whose vault was later replaced by an old
    # backup carrying in-vault state).
    state_dir = state_paths.vault_state_dir(vault)
    state_dir.mkdir(parents=True)
    (state_dir / ".graph-sync.json").write_bytes(b'{"epoch": "fresh-build"}')
    _reset_resolution_cache()

    with pytest.raises(state_migration.StatePlacementConflict) as caught:
        _migrate(vault)

    # The wire-visible message is content-free (refusal envelopes reach REST
    # and MCP verbatim); the paths ride on the exception's attributes and the
    # doctor section is the surface the spec requires to name both locations.
    text = str(caught.value)
    assert caught.value.external == state_dir
    assert caught.value.in_vault == vault / kb_dirname()
    assert str(state_dir) not in text, "the public refusal must stay content-free"
    assert str(vault) not in text, "the public refusal must stay content-free"
    assert "--migrate-state --offline --adopt-state" in text
    # Refusal, not resolution: both copies survive untouched.
    assert (state_dir / ".graph-sync.json").read_bytes() == b'{"epoch": "fresh-build"}'
    assert (vault / kb_dirname() / ".graph-sync.json").is_file()
    # And the refusal is wholesale: an external root of unknown provenance is
    # never merged into — no family was partially migrated and no manifest
    # bookkeeping was created beside the unrecognized state. The held offline
    # attempt may leave its regular, bounded coordination lock behind.
    for name, expected_bytes in members.items():
        source = vault / kb_dirname() / Path(name)
        assert source.is_file(), f"{name} left the vault although the placement was refused"
        assert source.read_bytes() == expected_bytes
    external_entries = {entry.name for entry in os.scandir(state_dir)}
    assert external_entries == {".graph-sync.json", state_migration._LOCK_NAME}, (
        f"the refused external root was written to: {sorted(external_entries)}"
    )
    lock = state_dir / state_migration._LOCK_NAME  # noqa: SLF001 - migration fixture
    assert lock.is_file()
    assert not lock.is_symlink()
    assert lock.stat().st_size <= 4096
    assert not (state_dir / state_migration.MANIFEST_NAME).exists()


def test_a_vault_with_neither_builds_fresh(tmp_path: Path) -> None:
    """Task 1.4 at the migration layer: adoption on a new machine."""
    from exomem import state_migration, state_paths
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    (vault / kb_dirname() / "Notes").mkdir(parents=True)
    (vault / kb_dirname() / "Notes" / "a-note.md").write_text("# note\n", encoding="utf-8")
    _reset_resolution_cache()

    resolution = _migrate(vault)

    assert resolution.state_dir == state_paths.vault_state_dir(vault)
    assert resolution.dual_state is False
    assert resolution.state_dir.is_dir(), "the fresh external root was not created"
    manifest = json.loads(
        (resolution.state_dir / state_migration.MANIFEST_NAME).read_text(encoding="utf-8")
    )
    assert manifest["state"] == "complete"
    assert manifest["vault_identity"] == state_paths.vault_state_key(vault)


def test_fresh_migration_caches_the_canonical_ready_resolution(tmp_path: Path) -> None:
    """A fresh root moves no bytes but is immediately admissible for startup."""
    from exomem import state_migration, state_paths
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    (vault / kb_dirname()).mkdir(parents=True)
    _reset_resolution_cache()

    migration = _migrate(vault)

    assert migration == state_migration.StateResolution(
        state_paths.vault_state_dir(vault), migrated=False, dual_state=False
    )
    assert state_migration.require_vault_state_ready(vault) == state_migration.StateResolution(
        state_paths.vault_state_dir(vault), migrated=True, dual_state=False
    )


def test_doctor_placement_is_ok_after_a_clean_migration(tmp_path: Path) -> None:
    from exomem import doctor
    _reset_resolution_cache()

    vault = tmp_path / "vault"
    _seed_state(vault)
    _migrate(vault)

    check = doctor._check_state_placement(vault)
    assert check.status == "pass", f"{check.message} / {check.details}"


def test_manifest_is_durable_in_progress_before_the_first_family_moves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import state_migration, state_paths

    vault = tmp_path / "vault"
    _seed_state(vault)
    _reset_resolution_cache()

    def stop_before_move(*_args, **_kwargs):
        state_dir = state_paths.vault_state_dir(vault)
        manifest = json.loads(
            (state_dir / state_migration.MANIFEST_NAME).read_text(encoding="utf-8")
        )
        assert manifest["state"] == "in-progress"
        assert manifest["vault_identity"] == state_paths.vault_state_key(vault)
        assert manifest["descriptors"]
        raise OSError("observed durable in-progress manifest")

    monkeypatch.setattr(state_migration, "_move_family", stop_before_move)
    with pytest.raises(OSError, match="observed durable"):
        _migrate(vault)


def test_unexplained_external_state_without_legacy_source_refuses(tmp_path: Path) -> None:
    from exomem import state_migration, state_paths
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    (vault / kb_dirname() / "Notes").mkdir(parents=True)
    state_dir = state_paths.ensure_vault_state_dir(vault)
    (state_dir / ".graph-sync.json").write_bytes(b"unexplained")
    _reset_resolution_cache()

    with pytest.raises(state_migration.StatePlacementConflict):
        _migrate(vault)

    assert (state_dir / ".graph-sync.json").read_bytes() == b"unexplained"


@pytest.mark.parametrize(
    "manifest_bytes",
    (
        b"{not json",
        b"[]",
        json.dumps(
            {
                "version": 999,
                "vault_identity": "untrusted",
                "descriptors": [],
                "state": "complete",
                "families": {},
            }
        ).encode(),
    ),
)
def test_invalid_or_newer_manifest_fails_closed(
    tmp_path: Path, manifest_bytes: bytes
) -> None:
    from exomem import state_migration, state_paths
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    (vault / kb_dirname() / "Notes").mkdir(parents=True)
    state_dir = state_paths.ensure_vault_state_dir(vault)
    (state_dir / state_migration.MANIFEST_NAME).write_bytes(manifest_bytes)
    _reset_resolution_cache()

    with pytest.raises(state_migration.StateMigrationManifestError):
        _migrate(vault)


def test_unreadable_manifest_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import state_migration, state_paths
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    (vault / kb_dirname() / "Notes").mkdir(parents=True)
    state_dir = state_paths.ensure_vault_state_dir(vault)
    (state_dir / state_migration.MANIFEST_NAME).write_text("{}", encoding="utf-8")
    _reset_resolution_cache()

    def unreadable(_path: Path) -> bytes:
        raise PermissionError("simulated unreadable manifest")

    monkeypatch.setattr(state_migration, "_read_manifest_bytes", unreadable)
    with pytest.raises(state_migration.StateMigrationManifestError):
        _migrate(vault)


def test_complete_manifest_descriptor_upgrade_migrates_new_family_first(
    tmp_path: Path,
) -> None:
    from exomem import state_migration, state_paths
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    (vault / kb_dirname() / "Notes").mkdir(parents=True)
    _reset_resolution_cache()
    _migrate(vault)
    state_dir = state_paths.vault_state_dir(vault)
    manifest_path = state_dir / state_migration.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["descriptors"].remove("claims-store")
    manifest["families"].pop("claims-store")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    legacy = vault / kb_dirname() / ".claims.sqlite"
    legacy.write_bytes(b"new-descriptor-state")
    _reset_resolution_cache()

    _migrate(vault)

    upgraded = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert upgraded["state"] == "complete"
    assert "claims-store" in upgraded["descriptors"]
    assert upgraded["families"]["claims-store"]["status"] == "complete"
    assert (state_dir / ".claims.sqlite").read_bytes() == b"new-descriptor-state"
    assert not legacy.exists()


def _remove_descriptor_from_complete_manifest(
    vault: Path,
    descriptor_id: str,
) -> tuple[Path, Path]:
    from exomem import state_migration, state_paths

    _reset_resolution_cache()
    _migrate(vault)
    state_dir = state_paths.vault_state_dir(vault)
    manifest_path = state_dir / state_migration.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["descriptors"].remove(descriptor_id)
    manifest["families"].pop(descriptor_id)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _reset_resolution_cache()
    return state_dir, manifest_path


def test_descriptor_upgrade_refuses_foreign_destination_only_bytes(
    tmp_path: Path,
) -> None:
    from exomem import state_migration
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    (vault / kb_dirname() / "Notes").mkdir(parents=True)
    state_dir, manifest_path = _remove_descriptor_from_complete_manifest(
        vault,
        "claims-store",
    )
    foreign = state_dir / ".claims.sqlite"
    foreign.write_bytes(b"foreign destination authority")

    with pytest.raises(state_migration.StatePlacementConflict) as caught:
        _migrate(vault)

    assert caught.value.code == "STATE_PLACEMENT_CONFLICT"
    assert foreign.read_bytes() == b"foreign destination authority"
    unchanged = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert unchanged["state"] == "complete"
    assert "claims-store" not in unchanged["descriptors"]


def test_descriptor_upgrade_accepts_destination_only_bytes_only_with_adoption(
    tmp_path: Path,
) -> None:
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    (vault / kb_dirname() / "Notes").mkdir(parents=True)
    state_dir, manifest_path = _remove_descriptor_from_complete_manifest(
        vault,
        "claims-store",
    )
    foreign = state_dir / ".claims.sqlite"
    foreign.write_bytes(b"explicitly adopted destination")

    resolution = _migrate(vault, adopt="external")

    assert resolution.state_dir == state_dir
    assert foreign.read_bytes() == b"explicitly adopted destination"
    upgraded = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert upgraded["state"] == "complete"
    assert upgraded["families"]["claims-store"]["status"] == "complete"
    assert upgraded["adopted"] == "external"


def test_descriptor_upgrade_accepts_destination_bytes_matching_legacy_source(
    tmp_path: Path,
) -> None:
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    (vault / kb_dirname() / "Notes").mkdir(parents=True)
    state_dir, manifest_path = _remove_descriptor_from_complete_manifest(
        vault,
        "claims-store",
    )
    legacy = vault / kb_dirname() / ".claims.sqlite"
    destination = state_dir / ".claims.sqlite"
    payload = b"matching crash-published destination"
    legacy.write_bytes(payload)
    destination.write_bytes(payload)

    _migrate(vault)

    assert not legacy.exists()
    assert destination.read_bytes() == payload
    upgraded = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert upgraded["families"]["claims-store"] == {"status": "complete"}


def test_descriptor_upgrade_preserves_the_both_empty_case(tmp_path: Path) -> None:
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    (vault / kb_dirname() / "Notes").mkdir(parents=True)
    _state_dir, manifest_path = _remove_descriptor_from_complete_manifest(
        vault,
        "claims-store",
    )

    _migrate(vault)

    upgraded = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert upgraded["state"] == "complete"
    assert upgraded["families"]["claims-store"] == {"status": "complete"}


def test_tree_removal_crash_replay_converges_a_resurrected_empty_legacy_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import state_migration, state_paths
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    legacy_tree = vault / kb_dirname() / ".graph-commit-receipts"
    legacy_tree.mkdir(parents=True)
    payload = b'{"receipt": 1}'
    (legacy_tree / "aaaaaaaaaaaaaaaaaaaaaaaa.json").write_bytes(payload)
    crashed = False

    def crash_after_tree_fence(point: str) -> None:
        nonlocal crashed
        if not crashed and point == "after-tree-removal":
            crashed = True
            raise OSError("simulated crash after durable tree removal")

    monkeypatch.setattr(state_migration, "_crash_point", crash_after_tree_fence)
    with pytest.raises(OSError, match="durable tree removal"):
        _migrate(vault)

    assert crashed
    state_dir = state_paths.vault_state_dir(vault)
    relocated = state_dir / ".graph-commit-receipts" / "aaaaaaaaaaaaaaaaaaaaaaaa.json"
    assert relocated.read_bytes() == payload
    assert not legacy_tree.exists()

    # A directory entry can reappear after a crash that preceded the parent
    # durability fence reaching stable storage. Retry must remove an empty
    # resurrection rather than classify it as a new authority.
    legacy_tree.mkdir()
    monkeypatch.setattr(state_migration, "_crash_point", lambda _point: None)
    _reset_resolution_cache()

    _migrate(vault)

    assert not legacy_tree.exists()
    assert state_migration.require_vault_state_ready(vault).state_dir == state_dir


@pytest.mark.parametrize(
    ("failure", "message"),
    (
        ("unlink", "could not remove"),
        ("flush", "could not flush"),
    ),
)
def test_empty_tree_removal_propagates_unlink_and_parent_flush_failures(
    failure: str,
    message: str,
) -> None:
    from exomem import held_fs, state_migration

    class Directory:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class Filesystem:
        def parent(self, _relative: str, *, access: str = "read"):
            del access
            return held_fs.HeldResult(value=Directory())

        def enumerate(self, _directory: Directory):
            return held_fs.HeldResult(value=())

        def children(self, _directory: Directory):
            return held_fs.HeldResult(value=())

        def unlink_directory(self, _directory: Directory):
            if failure == "unlink":
                return held_fs.HeldResult(
                    error=held_fs.HeldFsError("IO_REFUSED", "injected unlink")
                )
            return held_fs.HeldResult(value=None)

        def flush_directory(self, _directory: Directory):
            if failure == "flush":
                return held_fs.HeldResult(
                    error=held_fs.HeldFsError("IO_REFUSED", "injected flush")
                )
            return held_fs.HeldResult(value=None)

    with pytest.raises(OSError, match=message):
        state_migration._remove_empty_tree(
            Filesystem(),  # type: ignore[arg-type]
            "Knowledge Base/.graph-commit-receipts",
        )


@pytest.mark.parametrize(
    "crash_point",
    (
        "after-copy",
        "after-verification",
        "after-manifest-publish",
        "after-directory-fsync",
        "after-source-delete",
    ),
)
def test_every_migration_crash_cut_resumes_without_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
) -> None:
    from exomem import state_migration, state_paths
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    members = _seed_state(vault)
    _reset_resolution_cache()
    crashed = False

    def crash_once(point: str) -> None:
        nonlocal crashed
        if not crashed and point == crash_point:
            crashed = True
            raise OSError(f"simulated crash at {point}")

    monkeypatch.setattr(state_migration, "_crash_point", crash_once)
    with pytest.raises(OSError, match="simulated crash"):
        _migrate(vault)
    assert crashed, f"migration never exposed crash point {crash_point}"

    state_dir = state_paths.vault_state_dir(vault)
    for name, data in members.items():
        source = vault / kb_dirname() / Path(name)
        destination = state_dir / Path(name)
        observed = [path for path in (source, destination) if path.is_file()]
        assert observed, f"{name} vanished at {crash_point}"
        assert any(path.read_bytes() == data for path in observed)

    monkeypatch.setattr(state_migration, "_crash_point", lambda _point: None)
    _reset_resolution_cache()
    _migrate(vault)
    for name, data in members.items():
        assert (state_dir / Path(name)).read_bytes() == data
        assert not (vault / kb_dirname() / Path(name)).exists()


def test_destination_and_manifest_parent_directories_are_fsynced_before_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import state_migration, state_paths

    vault = tmp_path / "vault"
    _seed_state(vault)
    _reset_resolution_cache()
    synced: list[Path] = []
    deleted_after_sync: list[bool] = []
    real_sync = state_migration._fsync_directory
    real_delete = state_migration._delete_published_member

    def observe_sync(path: Path) -> None:
        real_sync(path)
        synced.append(Path(path))

    def observe_delete(*args, **kwargs):
        state_dir = state_paths.vault_state_dir(vault)
        deleted_after_sync.append(state_dir in synced)
        return real_delete(*args, **kwargs)

    monkeypatch.setattr(state_migration, "_fsync_directory", observe_sync)
    monkeypatch.setattr(
        state_migration,
        "_delete_published_member",
        observe_delete,
    )

    _migrate(vault)

    assert synced
    assert deleted_after_sync and all(deleted_after_sync)


def test_sqlite_main_and_journal_members_publish_as_one_family_before_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import state_migration, state_paths
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    kb = vault / kb_dirname()
    kb.mkdir(parents=True)
    for suffix, data in (("", b"main"), ("-wal", b"wal"), ("-shm", b"shm")):
        (kb / f".embeddings.sqlite{suffix}").write_bytes(data)
    _reset_resolution_cache()
    observed = False
    real_delete = state_migration._delete_published_member

    def observe_group(*args, **kwargs):
        nonlocal observed
        state_dir = state_paths.vault_state_dir(vault)
        if not observed:
            observed = True
            for suffix in ("", "-wal", "-shm"):
                assert (state_dir / f".embeddings.sqlite{suffix}").is_file()
            manifest = json.loads(
                (state_dir / state_migration.MANIFEST_NAME).read_text(encoding="utf-8")
            )
            family = manifest["families"]["embeddings-store"]
            assert family["status"] == "published"
            assert set(family["members"]) == {
                ".embeddings.sqlite",
                ".embeddings.sqlite-wal",
                ".embeddings.sqlite-shm",
            }
        return real_delete(*args, **kwargs)

    monkeypatch.setattr(
        state_migration,
        "_delete_published_member",
        observe_group,
    )
    _migrate(vault)
    assert observed


@pytest.mark.parametrize(
    ("member_name", "source", "destination"),
    (
        (".review-state.json", "../outside.json", ".review-state.json"),
        (".review-state.json", ".review-state.json", "../outside.json"),
        (".review-state.json", "/outside.json", ".review-state.json"),
        (".review-state.json", ".review-state.json", "C:" + "\\outside.json"),
        (".review-state.json", ".review-state.json", "nested/../outside.json"),
        (".review-state.json", ".review-state.json", "other.json"),
        ("nested//review.json", "nested/review.json", "nested/review.json"),
    ),
)
def test_published_manifest_member_paths_are_normalized_and_key_bound(
    tmp_path: Path,
    member_name: str,
    source: str,
    destination: str,
) -> None:
    from exomem import state_migration, state_paths

    vault = tmp_path / "vault"
    vault.mkdir()
    state_dir = state_paths.ensure_vault_state_dir(vault)
    manifest = {
        "version": state_migration.MANIFEST_VERSION,
        "vault_identity": state_paths.vault_state_key(vault),
        "descriptors": ["review-projection"],
        "state": "in-progress",
        "families": {
            "review-projection": {
                "status": "published",
                "members": {
                    member_name: {
                        "source": source,
                        "destination": destination,
                        "sha256": hashlib.sha256(b"outside proof").hexdigest(),
                        "size": len(b"outside proof"),
                        "identity": [1, 2, "file", 1],
                    }
                },
            }
        },
    }
    manifest_path = state_dir / state_migration.MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(state_migration.StateMigrationManifestError) as caught:
        state_migration._load_manifest(state_dir, vault_root=vault)

    assert caught.value.code == "STATE_MIGRATION_MANIFEST_INVALID"
    assert caught.value.path == manifest_path
    assert "published member" not in str(caught.value), (
        "the wire-visible migration refusal must stay content-free"
    )


def test_published_manifest_destination_cannot_escape_through_a_symlink(
    tmp_path: Path,
) -> None:
    from exomem import state_migration, state_paths

    vault = tmp_path / "vault"
    vault.mkdir()
    state_dir = state_paths.ensure_vault_state_dir(vault)
    outside = tmp_path / "outside-state"
    outside.mkdir()
    link = state_dir / "nested"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlinks are unavailable")
    payload = b"outside proof"
    (outside / "review.json").write_bytes(payload)
    member = "nested/review.json"
    record = {
        "source": member,
        "destination": member,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
        "identity": [1, 2, "file", 1],
    }

    with pytest.raises(state_migration.StateMigrationManifestError) as caught:
        state_migration._verify_published_records(state_dir, {member: record})

    assert caught.value.code == "STATE_MIGRATION_MANIFEST_INVALID"
    assert caught.value.path == Path(state_migration.MANIFEST_NAME)
    assert isinstance(caught.value.__cause__, ValueError)
    assert (outside / "review.json").read_bytes() == payload


def test_published_manifest_source_cannot_escape_through_a_symlink(
    tmp_path: Path,
) -> None:
    from exomem import state_migration
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    knowledge_base = vault / kb_dirname()
    knowledge_base.mkdir(parents=True)
    outside = tmp_path / "outside-vault"
    outside.mkdir()
    link = knowledge_base / "nested"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlinks are unavailable")
    payload = b"outside proof"
    (outside / "review.json").write_bytes(payload)
    member = "nested/review.json"
    record = {
        "source": member,
        "destination": member,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
        "identity": [1, 2, "file", 1],
    }

    with pytest.raises(state_migration.StateMigrationManifestError) as caught:
        state_migration._delete_published_member(
            object(),  # containment must fail before a held filesystem is needed
            vault,
            member,
            record,
        )

    assert caught.value.code == "STATE_MIGRATION_MANIFEST_INVALID"
    assert caught.value.path == Path(state_migration.MANIFEST_NAME)
    assert isinstance(caught.value.__cause__, ValueError)
    assert (outside / "review.json").read_bytes() == payload


def test_concurrent_migrators_serialize_one_family_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import state_migration

    vault = tmp_path / "vault"
    _seed_state(vault)
    _reset_resolution_cache()
    entered = threading.Event()
    release = threading.Event()
    moves = 0
    moves_lock = threading.Lock()
    real_move = state_migration._move_family

    def counted_move(*args, **kwargs):
        nonlocal moves
        with moves_lock:
            moves += 1
            first = moves == 1
        if first:
            entered.set()
            assert release.wait(timeout=5)
        return real_move(*args, **kwargs)

    monkeypatch.setattr(state_migration, "_move_family", counted_move)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_migrate, vault)
        assert entered.wait(timeout=5)
        second = pool.submit(_migrate, vault)
        release.set()
        first.result(timeout=20)
        second.result(timeout=20)

    descriptor_count = len(
        __import__(
            "exomem.reserved_paths", fromlist=["external_state_descriptors"]
        ).external_state_descriptors()
    )
    assert moves <= descriptor_count, "a second migrator repeated family work"


def test_target_adjacent_scratch_is_not_migrated(tmp_path: Path) -> None:
    from exomem import held_fs, state_paths
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    kb = vault / kb_dirname()
    notes = kb / "Notes"
    batch = notes / f".exomem-batch-{'a' * 32}"
    batch.mkdir(parents=True)
    (batch / "stage-0.tmp").write_bytes(b"active batch")
    held = kb / f"{held_fs.PUBLISH_TEMP_PREFIX}{'b' * 32}"
    held.write_bytes(b"active publication")
    (kb / ".due-state.json").write_bytes(b"{}")
    _reset_resolution_cache()

    _migrate(vault)

    assert (batch / "stage-0.tmp").read_bytes() == b"active batch"
    assert held.read_bytes() == b"active publication"
    state_dir = state_paths.vault_state_dir(vault)
    assert not (state_dir / batch.name).exists()
    assert not (state_dir / held.name).exists()


def test_fresh_deployment_admits_without_offline_migration(tmp_path: Path) -> None:
    """A provably-fresh deployment admits directly: zero bytes on either side.

    The docker onboarding path boots the server against a just-initialized
    vault with no external root and no manifest anywhere. There is nothing an
    offline stop window could protect, so the read-only gate bootstraps the
    first empty complete manifest itself instead of refusing.
    """
    from exomem import state_migration, state_paths
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    (vault / kb_dirname() / "Notes").mkdir(parents=True)
    (vault / kb_dirname() / "Notes" / "a-note.md").write_text("# note\n", encoding="utf-8")
    _reset_resolution_cache()

    resolution = state_migration.require_vault_state_ready(vault)

    assert resolution.state_dir == state_paths.vault_state_dir(vault)
    assert resolution.dual_state is False
    manifest = json.loads(
        (resolution.state_dir / state_migration.MANIFEST_NAME).read_text(encoding="utf-8")
    )
    assert manifest["state"] == "complete"
    assert manifest["vault_identity"] == state_paths.vault_state_key(vault)
    # The bootstrap is idempotent and the cached resolution stays admissible.
    assert state_migration.require_vault_state_ready(vault) == resolution


def test_admission_still_refuses_legacy_vault_state_without_manifest(tmp_path: Path) -> None:
    """Legacy in-vault bytes without a manifest keep the offline-required refusal."""
    from exomem import state_migration
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    kb = vault / kb_dirname()
    kb.mkdir(parents=True)
    (kb / ".governance.sqlite").write_bytes(b"legacy-governance-bytes")
    _reset_resolution_cache()

    with pytest.raises(state_migration.StateMigrationOfflineRequired):
        state_migration.require_vault_state_ready(vault)
    assert (kb / ".governance.sqlite").read_bytes() == b"legacy-governance-bytes"


def test_admission_still_refuses_external_state_without_manifest(tmp_path: Path) -> None:
    """External-root bytes without a manifest are unexplained: refuse, never bless."""
    from exomem import state_migration, state_paths
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    (vault / kb_dirname()).mkdir(parents=True)
    state_dir = state_paths.vault_state_dir(vault)
    state_dir.mkdir(parents=True)
    (state_dir / ".graph.sqlite").write_bytes(b"unexplained-external-bytes")
    _reset_resolution_cache()

    with pytest.raises(state_migration.StateMigrationOfflineRequired):
        state_migration.require_vault_state_ready(vault)
    assert (state_dir / ".graph.sqlite").read_bytes() == b"unexplained-external-bytes"


def test_bootstrap_fences_state_that_lands_after_the_manifest_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The migration lock is not exclusion of an older writer: bytes that land
    after the bootstrap's manifest write must roll the manifest back, never
    leave a false-complete authority whose remediation discards them."""
    from exomem import state_migration, state_paths
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    kb = vault / kb_dirname()
    kb.mkdir(parents=True)
    _reset_resolution_cache()

    real_scan = state_migration.scan_vault_state
    calls = {"n": 0}

    def racing_scan(vault_root: Path) -> dict[str, tuple[Path, ...]]:
        calls["n"] += 1
        if calls["n"] >= 3:  # pre-lock, under-lock pass; the fence sees the race
            return {"governance-store": (kb / ".governance.sqlite",)}
        return real_scan(vault_root)

    monkeypatch.setattr(state_migration, "scan_vault_state", racing_scan)

    with pytest.raises(state_migration.StateMigrationOfflineRequired):
        state_migration.require_vault_state_ready(vault)

    state_dir = state_paths.vault_state_dir(vault)
    assert not (state_dir / state_migration.MANIFEST_NAME).exists(), (
        "a lost old-writer race left a false-complete manifest behind"
    )


def test_bootstrap_under_lock_recheck_catches_state_between_scan_and_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 2.11's under-lock re-verification: an unclassified external entry
    that lands between the pre-lock scan and the lock keeps the refusal, and
    keeps it WITHOUT ever publishing a manifest — the write-fence would also
    retract, but publishing a complete authority over known-dirty state even
    transiently is what the under-lock check exists to prevent."""
    from exomem import state_migration, state_paths
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    (vault / kb_dirname()).mkdir(parents=True)
    state_dir = state_paths.vault_state_dir(vault)
    state_dir.mkdir(parents=True)
    _reset_resolution_cache()

    real_load = state_migration._load_manifest
    calls = {"n": 0}

    def racing_load(target: Path, *, vault_root: Path):
        calls["n"] += 1
        if calls["n"] == 2:  # the bootstrap's under-lock manifest read
            (Path(target) / "stray.bin").write_bytes(b"foreign-bytes")
        return real_load(target, vault_root=vault_root)

    monkeypatch.setattr(state_migration, "_load_manifest", racing_load)
    real_write = state_migration._write_manifest
    writes: list[Path] = []

    def spying_write(target: Path, manifest) -> None:
        writes.append(Path(target))
        real_write(target, manifest)

    monkeypatch.setattr(state_migration, "_write_manifest", spying_write)

    with pytest.raises(state_migration.StateMigrationOfflineRequired):
        state_migration.require_vault_state_ready(vault)

    assert not (state_dir / state_migration.MANIFEST_NAME).exists()
    assert (state_dir / "stray.bin").read_bytes() == b"foreign-bytes"
    assert writes == [], (
        "the under-lock re-check must refuse BEFORE any manifest is published"
    )


def test_orphaned_manifest_staging_temp_does_not_brick_fresh_admission(
    tmp_path: Path,
) -> None:
    """A crash during `_write_manifest` orphans `.{MANIFEST}.*.tmp` staging in
    the state root; that bookkeeping must never count as external state, or
    the crashed fresh deployment can never admit again."""
    from exomem import state_migration, state_paths
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    (vault / kb_dirname()).mkdir(parents=True)
    state_dir = state_paths.vault_state_dir(vault)
    state_dir.mkdir(parents=True)
    stray = state_dir / f".{state_migration.MANIFEST_NAME}.deadbeef.tmp"
    stray.write_bytes(b"interrupted staging bytes")
    _reset_resolution_cache()

    resolution = state_migration.require_vault_state_ready(vault)

    assert resolution.dual_state is False
    manifest = json.loads(
        (state_dir / state_migration.MANIFEST_NAME).read_text(encoding="utf-8")
    )
    assert manifest["state"] == "complete"


def test_bootstrap_refuses_promptly_when_the_migration_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Startup must never block unboundedly on the migration lock: contention
    refuses within the bounded budget instead of hanging the boot."""
    import time as time_module

    from exomem import state_migration, state_paths
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    (vault / kb_dirname()).mkdir(parents=True)
    state_dir = state_paths.vault_state_dir(vault)
    state_dir.mkdir(parents=True)
    _reset_resolution_cache()
    monkeypatch.setattr(state_migration, "_BOOTSTRAP_LOCK_TIMEOUT_SECONDS", 0.5)

    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with state_migration._migration_lock(state_dir):
            held.set()
            release.wait(timeout=30)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert held.wait(timeout=10), "lock holder thread never acquired the lock"
    try:
        started = time_module.monotonic()
        with pytest.raises(state_migration.StateMigrationOfflineRequired):
            state_migration.require_vault_state_ready(vault)
        elapsed = time_module.monotonic() - started
        assert elapsed < 5, f"bounded lock wait took {elapsed:.1f}s"
    finally:
        release.set()
        thread.join(timeout=10)


def test_bootstrap_retracts_the_manifest_when_the_fence_scan_itself_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fence scan that cannot run verified nothing: the just-published
    manifest must be retracted, never left as a false-complete authority."""
    from exomem import state_migration, state_paths
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    (vault / kb_dirname()).mkdir(parents=True)
    _reset_resolution_cache()

    real_scan = state_migration.scan_vault_state
    calls = {"n": 0}

    def failing_fence_scan(vault_root: Path) -> dict[str, tuple[Path, ...]]:
        calls["n"] += 1
        if calls["n"] >= 3:  # pre-lock and under-lock pass; the fence's scan dies
            raise OSError("vault became uninspectable during the fence")
        return real_scan(vault_root)

    monkeypatch.setattr(state_migration, "scan_vault_state", failing_fence_scan)

    with pytest.raises(state_migration.StateMigrationOfflineRequired):
        state_migration.require_vault_state_ready(vault)

    state_dir = state_paths.vault_state_dir(vault)
    assert not (state_dir / state_migration.MANIFEST_NAME).exists(), (
        "an unverifiable fence left a false-complete manifest behind"
    )


def test_bootstrap_that_cannot_publish_a_manifest_refuses_rather_than_recursing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-entry after a bootstrap is bounded: when the written manifest is
    not visible to the next read, the gate refuses instead of re-bootstrapping
    forever."""
    from exomem import state_migration
    from exomem.kbdir import kb_dirname

    vault = tmp_path / "vault"
    (vault / kb_dirname()).mkdir(parents=True)
    _reset_resolution_cache()

    monkeypatch.setattr(
        state_migration, "_write_manifest", lambda state_dir, manifest: None
    )

    with pytest.raises(state_migration.StateMigrationOfflineRequired):
        state_migration.require_vault_state_ready(vault)
