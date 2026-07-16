from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from exomem import deferred_index


def _embedding_sidecar(vault: Path) -> Path:
    return vault / "Knowledge Base" / ".embeddings.sqlite"


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

        monkeypatch.setattr(deferred_index.sqlite3, "connect", record_connect)

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
