from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from exomem import deferred_index, embedding_index, index_paths, semantic_index


def _embedding_sidecar(vault: Path) -> Path:
    return index_paths.sidecar_path(vault)


def _seed_coverage(conn: sqlite3.Connection, vault: Path, rels: list[str]) -> None:
    """Each seeded page has no occurrence; its coverage record proves that."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS semantic_unit_vectors "
        "(parent_path TEXT, parent_generation TEXT, unit_key TEXT)"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        [
            (
                embedding_index.COVERAGE_PREFIX + rel,
                embedding_index.coverage_record(semantic_index.build_parent_index_state(vault, rel)),
            )
            for rel in rels
        ],
    )


def _seed_embedding_rows(vault: Path, rows: list[tuple[str, float]]) -> None:
    sidecar = _embedding_sidecar(vault)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(sidecar)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS chunks ("
            "file_path TEXT NOT NULL, chunk_idx INTEGER NOT NULL, "
            "file_mtime REAL NOT NULL, PRIMARY KEY(file_path, chunk_idx))"
        )
        conn.executemany(
            "INSERT OR REPLACE INTO chunks(file_path, chunk_idx, file_mtime) "
            "VALUES (?, 0, ?)",
            rows,
        )
        _seed_coverage(conn, vault, [rel for rel, _mtime in rows])
        conn.commit()
    finally:
        conn.close()


def test_deferred_paths_are_durable_deduplicated_and_clearable(vault: Path) -> None:
    rel = "Knowledge Base/Notes/deferred.md"
    assert deferred_index.add(vault, [rel, rel]) == 1
    assert deferred_index.add(vault, [rel]) == 0
    assert deferred_index.status(vault)["count"] == 1

    # A fresh read from SQLite represents process restart recovery.
    assert deferred_index.list_paths(vault) == [rel]
    assert deferred_index.clear(vault, [rel]) == 1
    assert deferred_index.status(vault)["count"] == 0


def test_deferred_status_does_not_create_sidecar(vault: Path) -> None:
    path = deferred_index.store_path(vault)
    assert not path.exists()


def test_embedding_freshness_is_exact_and_tri_state(vault: Path) -> None:
    root = vault / "Knowledge Base" / "Notes"
    root.mkdir(parents=True, exist_ok=True)
    paths = [root / name for name in ("current.md", "ahead.md", "behind.md", "missing.md")]
    for index, path in enumerate(paths):
        path.write_text(f"# {index}\n", encoding="utf-8")
        info = path.stat()
        os.utime(path, ns=(info.st_atime_ns, 1_800_000_000_000_000_000 + index * 1_000_000))
    rels = [path.relative_to(vault).as_posix() for path in paths]
    mtimes = [path.stat().st_mtime for path in paths]
    _seed_embedding_rows(
        vault,
        [
            (rels[0], mtimes[0]),
            (rels[1], mtimes[1] + 0.1),
            (rels[2], mtimes[2] - 0.1),
        ],
    )

    result = deferred_index.inspect_embedding_freshness(vault, rels)

    assert result == {
        rels[0]: deferred_index.EmbeddingFreshness.CURRENT,
        rels[1]: deferred_index.EmbeddingFreshness.STALE,
        rels[2]: deferred_index.EmbeddingFreshness.STALE,
        rels[3]: deferred_index.EmbeddingFreshness.STALE,
    }
    sidecar = _embedding_sidecar(vault)
    assert not Path(f"{sidecar}-wal").exists()
    assert not Path(f"{sidecar}-shm").exists()


def test_freshness_inspection_imports_no_embedding_or_model_stack() -> None:
    code = (
        "import sys; import exomem.deferred_index; "
        "print(any(name == 'exomem.embeddings' or name == 'torch' "
        "or name.startswith('sentence_transformers') for name in sys.modules))"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "False"


def test_embedding_freshness_uses_percent_safe_uri_and_live_wal_without_changes(
    tmp_path: Path, monkeypatch
) -> None:
    vault = tmp_path / "vault #% üll"
    note = vault / "Knowledge Base" / "Notes" / "current.md"
    note.parent.mkdir(parents=True)
    note.write_text("# current\n", encoding="utf-8")
    rel = note.relative_to(vault).as_posix()
    sidecar = _embedding_sidecar(vault)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    writer = sqlite3.connect(sidecar)
    real_connect = sqlite3.connect
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute(
            "CREATE TABLE chunks (file_path TEXT, chunk_idx INTEGER, file_mtime REAL)"
        )
        writer.execute(
            "INSERT INTO chunks(file_path, chunk_idx, file_mtime) VALUES (?, 0, ?)",
            (rel, note.stat().st_mtime),
        )
        _seed_coverage(writer, vault, [rel])
        writer.commit()
        companions = [sidecar, Path(f"{sidecar}-wal"), Path(f"{sidecar}-shm")]
        assert all(path.exists() for path in companions)
        before = {
            path: (path.read_bytes(), path.stat())
            for path in companions
        }
        calls: list[str] = []

        def record_connect(database, *args, **kwargs):
            calls.append(str(database))
            return real_connect(database, *args, **kwargs)

        monkeypatch.setattr(deferred_index, "_sqlite_connect_owned", record_connect)

        result = deferred_index.inspect_embedding_freshness(vault, [rel])

        assert result[rel] is deferred_index.EmbeddingFreshness.CURRENT
        assert len(calls) == 1
        assert calls[0].startswith("file:///")
        assert calls[0].endswith("/.embeddings.sqlite?mode=ro")
        for path, (content, info) in before.items():
            after = path.stat()
            assert path.read_bytes() == content
            assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) == (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
            )
    finally:
        writer.close()


def test_embedding_freshness_batches_beyond_sqlite_variable_limit(vault: Path) -> None:
    root = vault / "Knowledge Base" / "Notes"
    root.mkdir(parents=True, exist_ok=True)
    rels: list[str] = []
    rows: list[tuple[str, float]] = []
    for index in range(1_050):
        path = root / f"note-{index:04d}.md"
        path.write_text("# note\n", encoding="utf-8")
        rel = path.relative_to(vault).as_posix()
        rels.append(rel)
        rows.append((rel, path.stat().st_mtime))
    _seed_embedding_rows(vault, rows)

    result = deferred_index.inspect_embedding_freshness(vault, rels)

    assert len(result) == len(rels)
    assert set(result.values()) == {deferred_index.EmbeddingFreshness.CURRENT}


def test_embedding_freshness_conservatively_unverifiable_sidecars(vault: Path) -> None:
    note = vault / "Knowledge Base" / "Notes" / "note.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# note\n", encoding="utf-8")
    rel = note.relative_to(vault).as_posix()
    sidecar = _embedding_sidecar(vault)

    missing = deferred_index.inspect_embedding_freshness(vault, [rel])
    assert missing[rel] is deferred_index.EmbeddingFreshness.UNVERIFIABLE

    sidecar.write_bytes(b"not sqlite")
    corrupt = deferred_index.inspect_embedding_freshness(vault, [rel])
    assert corrupt[rel] is deferred_index.EmbeddingFreshness.UNVERIFIABLE


def test_embedding_freshness_busy_rollback_journal_is_unverifiable(
    vault: Path,
) -> None:
    note = vault / "Knowledge Base" / "Notes" / "busy.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# busy\n", encoding="utf-8")
    rel = note.relative_to(vault).as_posix()
    _seed_embedding_rows(vault, [(rel, note.stat().st_mtime)])
    sidecar = _embedding_sidecar(vault)
    writer = sqlite3.connect(sidecar)
    try:
        writer.execute("BEGIN EXCLUSIVE")
        writer.execute(
            "UPDATE chunks SET file_mtime = file_mtime + 1 WHERE file_path = ?",
            (rel,),
        )
        assert Path(f"{sidecar}-journal").exists()

        result = deferred_index.inspect_embedding_freshness(vault, [rel])

        assert result[rel] is deferred_index.EmbeddingFreshness.UNVERIFIABLE
    finally:
        writer.rollback()
        writer.close()


def test_revisioned_receipt_conditional_clear_preserves_newer_work(vault: Path) -> None:
    rel = "Knowledge Base/Notes/revisioned.md"
    [first] = deferred_index.add_receipts(vault, [rel])
    [second] = deferred_index.add_receipts(vault, [rel])

    assert first.revision == 1
    assert second.revision == 2
    assert deferred_index.clear_receipts(vault, [first]) == 0
    assert deferred_index.snapshot(vault) == [second]
    assert deferred_index.clear_receipts(vault, [second]) == 1
    assert deferred_index.snapshot(vault) == []


@pytest.mark.parametrize("generation", [None, 22])
def test_graph_settlement_cannot_touch_a_new_receipt_for_the_same_path(
    vault: Path, generation: int | None,
) -> None:
    old, peer = deferred_index.add_graph_receipts(
        vault, ["Knowledge Base/Notes/a.md", "Knowledge Base/Notes/b.md"], generation=generation,
    )
    assert deferred_index.clear_graph_receipts(vault, [old]) == 1
    (current,) = deferred_index.add_graph_receipts(vault, [old.rel_path], generation=generation)
    assert current.graph_generation == old.graph_generation
    assert current.revision > old.revision
    assert deferred_index.note_graph_failure(vault, old, cause="unreadable") is None
    assert not deferred_index.quarantine_graph_receipt(vault, old, signature=None)
    assert deferred_index.rotate_graph_receipts(vault, [old]) == 0
    current_failure = deferred_index.note_graph_failure(vault, current, cause="unreadable")
    assert current_failure is not None and current_failure[0] == 1
    peer_failure = deferred_index.note_graph_failure(vault, peer, cause="unreadable")
    assert peer_failure is not None and peer_failure[0] == 1

    assert deferred_index.clear_graph_receipts(vault, [old, peer]) == 1
    assert deferred_index.snapshot_graph(vault) == [current]
    assert deferred_index.graph_receipts_in_read_grace(vault, seconds=600)
    (requeued,) = deferred_index.add_graph_receipts(vault, [current.rel_path], generation=generation)
    assert not deferred_index.graph_receipts_in_read_grace(vault, seconds=600)
    requeued_failure = deferred_index.note_graph_failure(vault, requeued, cause="unreadable")
    assert requeued_failure == (2, current_failure[1])
    (peer_again,) = deferred_index.add_graph_receipts(vault, [peer.rel_path], generation=generation)
    retried_failure = deferred_index.note_graph_failure(vault, peer_again, cause="unreadable")
    assert retried_failure is not None and retried_failure[0] == 1


def test_graph_receipt_migration_reserves_legacy_receipt_and_failure_revisions(vault: Path) -> None:
    live, held = deferred_index.add_graph_receipts(
        vault, ["Knowledge Base/Notes/a.md", "Knowledge Base/Notes/b.md"], generation=4,
    )
    assert deferred_index.quarantine_graph_receipt(vault, held, signature="retained")
    with sqlite3.connect(deferred_index.store_path(vault)) as conn:
        conn.execute("DROP INDEX IF EXISTS graph_upserts_queue_order")
        conn.execute("UPDATE graph_upserts SET revision = 100 WHERE rel_path = ?", (live.rel_path,))
        conn.execute(
            "UPDATE graph_failures SET failure_revision = 200 WHERE rel_path = ?", (held.rel_path,),
        )

    (new,) = deferred_index.add_graph_receipts(vault, ["Knowledge Base/Notes/c.md"])
    assert new.revision > 200
    assert deferred_index.snapshot_graph(vault)[0] == deferred_index.DeferredReceipt(live.rel_path, 100, 4)
    with sqlite3.connect(deferred_index.store_path(vault)) as conn:
        assert conn.execute(
            "SELECT failure_revision FROM graph_failures WHERE rel_path = ?", (held.rel_path,),
        ).fetchone() == (200,)


def test_receipt_schema_migrates_and_vaults_are_isolated(tmp_path: Path) -> None:
    first_vault = tmp_path / "first"
    second_vault = tmp_path / "second"
    legacy = deferred_index.store_path(first_vault)
    legacy.parent.mkdir(parents=True)
    conn = sqlite3.connect(legacy)
    try:
        conn.execute(
            "CREATE TABLE semantic_upserts ("
            "rel_path TEXT PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO semantic_upserts VALUES ('Knowledge Base/Notes/legacy.md', 1, 1)"
        )
        conn.commit()
    finally:
        conn.close()

    [migrated] = deferred_index.add_receipts(
        first_vault, ["Knowledge Base/Notes/legacy.md"]
    )
    [other] = deferred_index.add_receipts(
        second_vault, ["Knowledge Base/Notes/legacy.md"]
    )

    assert migrated.revision == 2
    assert other.revision == 1
    assert deferred_index.clear_receipts(first_vault, [migrated]) == 1
    assert deferred_index.snapshot(first_vault) == []
    assert deferred_index.snapshot(second_vault) == [other]
