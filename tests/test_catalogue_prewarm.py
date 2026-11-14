"""Staged catalogues preserve serving state and reconcile across process boundaries."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from exomem import catalogue_prewarm as prewarm
from exomem import freshness, lexstore, reserved_paths


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    kb = root / "Knowledge Base"
    kb.mkdir(parents=True)
    (kb / "page.md").write_text("---\ntype: insight\ntitle: Example\n---\n# Example\n\nOriginal text.\n")
    lexstore.clear_stores()
    freshness.clear()
    yield root
    lexstore.clear_stores()
    freshness.clear()


def _stage(vault):
    assert prewarm.build(vault) == "staged"
    store = lexstore.LexicalStore(vault)
    return store, prewarm._staged_copies(store)[0]


def _tree(root):
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_cli_build_leaves_every_unrelated_shared_file_byte_identical(vault):
    """Target startup must not migrate other stores or initialize the running cell."""
    store = lexstore.LexicalStore(vault)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    other = store.path.parent / "unrelated.sqlite"
    other.write_bytes(b"unrelated state")
    lexstore.ensure_fresh(vault)
    conn = sqlite3.connect(store.path)
    with conn:
        conn.execute("UPDATE meta SET value='previous-image' WHERE key='catalog_identity'")
    conn.close()
    before_vault, before_state = _tree(vault), _tree(store.path.parent)
    run = subprocess.run(
        [sys.executable, "-m", "exomem.catalogue_prewarm", "--vault", str(vault)],
        capture_output=True, text=True, timeout=60,
    )
    assert run.returncode == 0, run.stderr
    assert "staged" in run.stdout
    assert _tree(vault) == before_vault
    after = _tree(store.path.parent)
    assert {p: after[p] for p in before_state} == before_state
    assert all(
        reserved_paths.state_target_descriptor_id(vault, store.path.parent / p)
        in {"lexical-staged", "lexical-staged-lock"}
        for p in after.keys() - before_state.keys()
    )


def test_repeated_build_keeps_complete_copy_and_live_identity_gate_avoids_parsing(vault, monkeypatch):
    """Retrying the target image must neither duplicate a staged copy nor parse a current live catalogue."""
    store, path = _stage(vault)
    identity = lexstore.catalog_semantic_identity(vault)
    assert prewarm._catalog_valid(store, path, "lexical-staged", identity)
    original = path.read_bytes()
    superseded = path.with_name(".lexical-staged-" + "f" * 16 + ".sqlite")
    superseded.write_bytes(b"previous target")
    monkeypatch.setattr(lexstore.LexicalStore, "build_detached_catalog", lambda self: pytest.fail("duplicate parse"))
    assert prewarm.build(vault) == "already-staged"
    assert path.read_bytes() == original
    assert not superseded.exists()
    assert prewarm.adopt_at_boot(vault)
    conn = sqlite3.connect(store.path)
    with conn:
        conn.execute("DELETE FROM meta WHERE key LIKE 'recall_checkpoint:%'")
    conn.close()
    # Only staged adoption needs checkpoints; the live self-gate depends on its stored identity.
    assert prewarm.build(vault) == "already-live"


def test_second_process_declines_without_replacing_stable_build_lock(vault):
    """A second pod must not remove files owned by an active builder or split the stable lock inode."""
    store = lexstore.LexicalStore(vault)
    with prewarm._build_lock(store):
        lock = store.path.parent / reserved_paths.LEXICAL_STAGED_LOCK
        inode = lock.stat().st_ino
        run = subprocess.run(
            [sys.executable, "-m", "exomem.catalogue_prewarm", "--vault", str(vault)],
            capture_output=True, text=True, timeout=30,
        )
        assert run.returncode == 0, run.stderr
        assert "busy" in run.stdout
        assert not prewarm._staged_copies(store)
    with prewarm._build_lock(store):
        assert lock.stat().st_ino == inode


def test_store_mode_entrypoint_changes_no_shared_file(vault):
    """A store-mode cell must not create a file catalogue or run another store's migrations."""
    from conftest import initialize_vault_state_offline

    from exomem import state_migration
    from exomem.collection_store import connection

    initialize_vault_state_offline(vault, source="prewarm store-mode fixture")
    with connection.open_writer(connection.store_path(vault), lease_check=lambda: True) as writer:
        with writer.transaction() as tx:
            tx.execute("INSERT INTO store_mode(id, mode, revision, changed_at, release) "
                       "VALUES (1, 'store', 1, '2026-01-01T00:00:00Z', 'test')")
    state_migration.record_vault_store_compatibility(vault, authority_check=lambda: True, active=True)
    before_state = _tree(lexstore.lexical_path(vault).parent)
    before = _tree(vault.parent)
    assert prewarm.build(vault) == "store-mode-or-transition"
    assert not prewarm.adopt_at_boot(vault)
    assert _tree(vault.parent) == before
    assert _tree(lexstore.lexical_path(vault).parent) == before_state


@pytest.mark.parametrize("invalid", ["corrupt", "identity", "checkpoint", "schema", "wal"])
def test_invalid_stage_is_observational_then_discarded_for_cold_fallback(vault, invalid):
    """A corrupt, incompatible or incomplete copy must never replace the live SQLite set."""
    store, path = _stage(vault)
    if invalid == "corrupt":
        path.write_bytes(b"invalid sqlite")
    elif invalid == "wal":
        Path(f"{path}-wal").write_bytes(b"unfinished WAL")
    else:
        conn = sqlite3.connect(path)
        with conn:
            if invalid == "identity":
                conn.execute("UPDATE meta SET value='another-image' WHERE key='catalog_identity'")
            elif invalid == "checkpoint":
                conn.execute("DELETE FROM meta WHERE key LIKE 'recall_checkpoint:%'")
            else:
                conn.execute("PRAGMA user_version=0")
                conn.execute("UPDATE meta SET value='0' WHERE key='schema_version'")
        conn.close()
    before = _tree(store.path.parent)
    assert not prewarm._catalog_valid(store, path, "lexical-staged", lexstore.catalog_semantic_identity(vault))
    assert _tree(store.path.parent) == before
    assert not prewarm.adopt_at_boot(vault)
    assert not prewarm._staged_copies(store)
    lexstore.ensure_fresh(vault)
    assert store.path.exists()


def test_adopted_copy_uses_restart_heal_for_edits_additions_and_deletions(vault, monkeypatch):
    """A build from another process must heal ordinary drift without parsing the whole corpus."""
    kb = vault / "Knowledge Base"
    (kb / "gone.md").write_text("# Removed\n")
    (kb / "kept.md").write_text("# Kept\n")
    store, path = _stage(vault)
    (kb / "page.md").write_text("# Edited\n\nChanged text.\n")
    (kb / "gone.md").unlink()
    (kb / "added.md").write_text("# Added\n")
    lexstore.clear_stores()
    freshness.clear()
    assert prewarm.adopt_at_boot(vault)
    monkeypatch.setattr(lexstore.LexicalStore, "_rebuild", lambda *args: pytest.fail("full parse"))
    lexstore.ensure_fresh(vault)
    with sqlite3.connect(store.path) as conn:
        names = {row[0] for row in conn.execute("SELECT path FROM pages")}
        assert names == {"Knowledge Base/page.md", "Knowledge Base/kept.md", "Knowledge Base/added.md"}
        assert conn.execute("SELECT title FROM pages WHERE path LIKE '%/page.md'").fetchone()[0] == 'Edited'
    assert not path.exists()


def test_killed_pods_wal_cannot_attach_to_adopted_rows(vault):
    """A killed serving pod's committed WAL must never overwrite the adopted generation."""
    store, path = _stage(vault)
    # Exit without sqlite.close(), leaving a real committed WAL beside the old main.
    code = """import os, sqlite3, sys
c=sqlite3.connect(sys.argv[1]); c.execute('PRAGMA journal_mode=WAL')
c.execute('CREATE TABLE old_generation(value)'); c.commit()
c.execute('INSERT INTO old_generation VALUES (42)'); c.commit(); os._exit(0)
"""
    run = subprocess.run([sys.executable, "-c", code, str(store.path)], timeout=30)
    assert run.returncode == 0 and Path(f"{store.path}-wal").exists()
    assert prewarm.adopt_at_boot(vault)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM pages").fetchone()[0] > 0
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='old_generation'").fetchone()


def test_failed_quarantine_install_restores_the_previous_whole_set(vault, monkeypatch):
    """A failed staged move must restore all prior generation members, including orphan sidecars."""
    store, path = _stage(vault)
    Path(f"{store.path}-wal").write_bytes(b"old WAL")
    Path(f"{store.path}-shm").write_bytes(b"old SHM")
    real = reserved_paths._move_owner_file
    injected = []
    def fail_stage(root, source, owner, *args, **kwargs):
        if owner == "lexical-staged":
            injected.append(True)
            raise OSError("injected install failure")
        return real(root, source, owner, *args, **kwargs)
    monkeypatch.setattr(reserved_paths, "_move_owner_file", fail_stage)
    assert not prewarm.adopt_at_boot(vault)
    assert injected
    assert not store.path.exists()
    assert Path(f"{store.path}-wal").read_bytes() == b"old WAL"
    assert Path(f"{store.path}-shm").read_bytes() == b"old SHM"


def test_startup_preserves_old_main_with_recent_wal_and_held_build_families(vault):
    """An old main timestamp must not let cleanup remove an active or recently written WAL family."""
    store = lexstore.LexicalStore(vault)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    temp = store.path.with_name(f"{store.path.name}.rebuild-{uuid.uuid4().hex}.tmp")
    temp.write_bytes(b"building")
    old = time.time() - lexstore._ORPHAN_REBUILD_TEMP_AGE_SECONDS - 10
    os.utime(temp, (old, old))
    wal = Path(f"{temp}-wal")
    wal.write_bytes(b"recent WAL")
    assert not store._sweep_orphan_rebuild_temps()
    os.utime(wal, (old, old))
    lock = lexstore._RebuildTempLock(temp)
    lock.acquire()
    try:
        assert not store._sweep_orphan_rebuild_temps(boot=True)
        assert temp.exists() and wal.exists()
    finally:
        lock.release()
    store._sweep_orphan_rebuild_temps(boot=True)
    assert not temp.exists() and not wal.exists()


def test_boot_removes_duplicates_superseded_stages_and_recent_cancelled_temps(vault):
    """Boot must not retain matching duplicates or recent files from a cancelled build."""
    store, stage = _stage(vault)
    duplicate = stage.with_name(".lexical-staged-" + "0" * 16 + ".sqlite")
    duplicate.write_bytes(stage.read_bytes())
    superseded = stage.with_name(".lexical-staged-" + "1" * 16 + ".sqlite")
    superseded.write_bytes(b"obsolete")
    temp = store.path.with_name(f"{store.path.name}.rebuild-{uuid.uuid4().hex}.tmp")
    temp.write_bytes(b"recent cancelled build")
    assert prewarm.adopt_at_boot(vault)
    assert not prewarm._staged_copies(store) and not temp.exists()


@pytest.mark.skipif(os.name != "posix", reason="Cloud build Jobs run on Linux")
def test_killed_build_never_leaves_a_torn_adoptable_copy(vault):
    """Killing a builder while it parses must leave only locked temporary names, never a staged copy."""
    import select

    code = """import sys, time
from pathlib import Path
from exomem import catalogue_prewarm, lexstore
original = lexstore.LexicalStore._insert_page
def pause(self, *args, **kwargs):
    original(self, *args, **kwargs)
    print('building', flush=True)
    time.sleep(60)
lexstore.LexicalStore._insert_page = pause
catalogue_prewarm.build(Path(sys.argv[1]))
"""
    process = subprocess.Popen([sys.executable, "-c", code, str(vault)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert select.select([process.stdout], [], [], 30)[0], "builder did not reach its first parsed page"
        assert process.stdout.readline().strip() == b"building"
        store = lexstore.LexicalStore(vault)
        assert not prewarm._staged_copies(store)
        assert list(store.path.parent.glob(".lexical.sqlite.rebuild-*.tmp*"))
    finally:
        process.kill()
        process.communicate(timeout=15)
    assert not prewarm.adopt_at_boot(vault)
    assert not list(store.path.parent.glob(".lexical.sqlite.rebuild-*.tmp*"))


def test_runtime_catalogue_names_remain_covered_by_controller_backup_exclusions(vault, monkeypatch):
    """A cell filename change must not silently send disposable catalogue bytes into backups."""
    import fnmatch

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "infra" / "cellctl" / "src"))
    from cellctl.manifests import CATALOGUE_BACKUP_EXCLUDES

    store, stage = _stage(vault)
    assert reserved_paths.state_target_descriptor_id(vault, stage) == "lexical-staged"
    for suffix in reserved_paths._SQLITE_SUFFIXES:
        staged = "/data/host/.local/state/exomem/state/vault-key/" + stage.name + suffix
        assert any(fnmatch.fnmatchcase(staged, pattern) for pattern in CATALOGUE_BACKUP_EXCLUDES)
