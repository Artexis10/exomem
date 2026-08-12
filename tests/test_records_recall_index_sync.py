from __future__ import annotations

import sqlite3
from pathlib import Path

from exomem import deferred_index, index_sync, recall_policy, sidecar_store


def _records_manifest() -> str:
    return """---
type: collection
exomem_id: 12345678-1234-4abc-8def-123456789abc
title: Measurements
semantic_profile: records
collection_version: 1
lifecycle: active
schema_version: 1
storage:
  strategy: markdown-items
  format_version: 1
  source: items
item_schema:
  natural_key: [observed]
  fields:
    observed:
      type: string
---
"""


def test_recall_batch_partitions_one_guarded_markdown_snapshot(tmp_path: Path) -> None:
    note = tmp_path / "Knowledge Base" / "Notes" / "note.md"
    manifest = tmp_path / "Knowledge Base" / "Records" / "Health" / "_collection.md"
    raw = manifest.parent / "items" / "raw.md"
    note.parent.mkdir(parents=True)
    raw.parent.mkdir(parents=True)
    note.write_text("ordinary note", encoding="utf-8")
    manifest.write_text(_records_manifest(), encoding="utf-8")
    raw.write_text("private measurement", encoding="utf-8")

    batch = recall_policy.partition_markdown_paths(
        tmp_path, [raw, note, manifest, raw, tmp_path / "missing.md", tmp_path / "bad.txt"]
    )

    assert [item.rel_path for item in batch.identity_paths] == [
        "Knowledge Base/Records/Health/items/raw.md",
        "Knowledge Base/Notes/note.md",
        "Knowledge Base/Records/Health/_collection.md",
    ]
    assert [item.rel_path for item in batch.admitted_paths] == [
        "Knowledge Base/Notes/note.md",
        "Knowledge Base/Records/Health/_collection.md",
    ]
    assert [item.rel_path for item in batch.suppressed_paths] == [
        "Knowledge Base/Records/Health/items/raw.md"
    ]
    assert batch.missing_paths == ("missing.md",)
    assert batch.invalid_paths == ("bad.txt",)
    assert batch.revalidate(tmp_path)

    raw.write_text("direct manual edit", encoding="utf-8")
    assert not batch.revalidate(tmp_path)


def test_recall_batch_never_reads_a_suppressed_record_body(
    tmp_path: Path, monkeypatch
) -> None:
    raw = tmp_path / "Knowledge Base" / "Records" / "Health" / "items" / "raw.md"
    raw.parent.mkdir(parents=True)
    raw.write_text("private measurement", encoding="utf-8")
    real_read = recall_policy.vault.read_guarded_text

    def reject_raw(_root: Path, path: Path):
        if path == raw:
            raise AssertionError("raw Record body must not be opened")
        return real_read(_root, path)

    monkeypatch.setattr(recall_policy.vault, "read_guarded_text", reject_raw)

    batch = recall_policy.partition_markdown_paths(tmp_path, [raw])

    assert [item.rel_path for item in batch.suppressed_paths] == [
        "Knowledge Base/Records/Health/items/raw.md"
    ]
    assert batch.revalidate(tmp_path)


def test_index_sync_routes_identity_and_semantic_paths_separately(
    tmp_path: Path, monkeypatch
) -> None:
    from exomem import embeddings, epistemic_graph, find, lexstore, memory_refs

    note = tmp_path / "Knowledge Base" / "Notes" / "note.md"
    manifest = tmp_path / "Knowledge Base" / "Records" / "Health" / "_collection.md"
    raw = manifest.parent / "items" / "raw.md"
    note.parent.mkdir(parents=True)
    raw.parent.mkdir(parents=True)
    note.write_text("ordinary note", encoding="utf-8")
    manifest.write_text(_records_manifest(), encoding="utf-8")
    raw.write_text("private measurement", encoding="utf-8")
    seen: dict[str, list[list[str]]] = {}

    def capture(name: str):
        def _capture(_root, paths, *_args, **_kwargs):
            seen.setdefault(name, []).append(
                [path.as_posix() if isinstance(path, Path) else str(path) for path in paths]
            )

        return _capture

    monkeypatch.setattr(memory_refs, "upsert_after_write", capture("memory_refs"))
    monkeypatch.setattr(lexstore, "upsert_after_write", capture("lexstore"))
    monkeypatch.setattr(epistemic_graph, "upsert_after_write", capture("graph"))
    monkeypatch.setattr(
        embeddings,
        "upsert_after_write_status",
        lambda _root, paths: capture("embeddings")(_root, paths)
        or embeddings.EmbeddingSyncStatus("completed", "embedding_upsert_completed", len(paths)),
    )
    monkeypatch.setattr(
        find,
        "on_resolver_files_changed",
        lambda _root, changed, _deleted: seen.setdefault("resolver", []).append(changed),
    )
    monkeypatch.setattr(index_sync, "purge_semantic_only", capture("purge"))

    index_sync.upsert_after_write(tmp_path, [raw, note, manifest])

    assert seen["memory_refs"] == [[raw.as_posix(), note.as_posix(), manifest.as_posix()]]
    assert seen["resolver"] == [[
        "Knowledge Base/Records/Health/items/raw.md",
        "Knowledge Base/Notes/note.md",
        "Knowledge Base/Records/Health/_collection.md",
    ]]
    assert seen["lexstore"] == [[note.as_posix(), manifest.as_posix()]]
    assert seen["graph"] == [[note.as_posix(), manifest.as_posix()]]
    assert seen["embeddings"] == [[note.as_posix(), manifest.as_posix()]]
    assert seen["purge"] == [["Knowledge Base/Records/Health/items/raw.md"]]


def test_deferred_semantic_receipts_purge_live_suppression_with_revision_cas(
    tmp_path: Path,
) -> None:
    note = tmp_path / "Knowledge Base" / "Notes" / "note.md"
    raw = tmp_path / "Knowledge Base" / "Records" / "Health" / "items" / "raw.md"
    note.parent.mkdir(parents=True)
    raw.parent.mkdir(parents=True)
    note.write_text("ordinary", encoding="utf-8")
    raw.write_text("private", encoding="utf-8")
    note_rel = note.relative_to(tmp_path).as_posix()
    raw_rel = raw.relative_to(tmp_path).as_posix()
    assert deferred_index.add(tmp_path, [note_rel]) == 1
    with sqlite3.connect(deferred_index.store_path(tmp_path)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO semantic_upserts(rel_path, created_at, updated_at, revision) "
                "VALUES (?, 1, 1, 7)",
                (raw_rel,),
            )

    assert deferred_index.snapshot(tmp_path) == [deferred_index.DeferredReceipt(note_rel, 1)]
    with sqlite3.connect(deferred_index.store_path(tmp_path)) as conn:
        assert conn.execute(
            "SELECT rel_path FROM semantic_upserts WHERE rel_path = ?", (raw_rel,)
        ).fetchone() is None


def test_sidecar_policy_identity_helpers_support_meta_and_graph_meta() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value INTEGER)")
        conn.execute("CREATE TABLE graph_meta(key TEXT PRIMARY KEY, value TEXT)")
        sidecar_store.write_recall_policy_identity(conn, "v1", "access-a")
        sidecar_store.write_recall_policy_identity(
            conn, "v2", "access-b", table="graph_meta"
        )

        assert sidecar_store.read_recall_policy_identity(conn) == ("v1", "access-a")
        assert sidecar_store.read_recall_policy_identity(conn, table="graph_meta") == (
            "v2",
            "access-b",
        )
    finally:
        conn.close()


def test_full_deferred_queue_purges_unsafe_legacy_identity(tmp_path: Path) -> None:
    note = tmp_path / "Knowledge Base" / "Notes" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text("ordinary", encoding="utf-8")
    note_rel = note.relative_to(tmp_path).as_posix()
    assert deferred_index.add_full(tmp_path, [note_rel]) == 1
    with sqlite3.connect(deferred_index.store_path(tmp_path)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO full_upserts(rel_path, created_at, updated_at) VALUES (?, 1, 1)",
                ("../../outside.md",),
            )
            conn.execute(
                "INSERT INTO full_upserts(rel_path, created_at, updated_at) VALUES (?, 1, 1)",
                ("Knowledge Base\\Notes\\legacy.md",),
            )

    assert deferred_index.list_full_paths(tmp_path) == [note_rel]
    with sqlite3.connect(deferred_index.store_path(tmp_path)) as conn:
        assert conn.execute(
            "SELECT rel_path FROM full_upserts WHERE rel_path = '../../outside.md'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT rel_path FROM full_upserts WHERE rel_path = ?",
            ("Knowledge Base\\Notes\\legacy.md",),
        ).fetchone() is None


def test_index_sync_reports_a_failed_semantic_purge_for_reconcile(
    tmp_path: Path, monkeypatch
) -> None:
    from exomem import embeddings, epistemic_graph, find, lexstore, memory_refs

    page = tmp_path / "Knowledge Base" / "Notes" / "note.md"
    page.parent.mkdir(parents=True)
    page.write_text("ordinary", encoding="utf-8")
    for module in (lexstore, memory_refs, epistemic_graph):
        monkeypatch.setattr(module, "upsert_after_write", lambda *_args: None)
    monkeypatch.setattr(find, "on_resolver_files_changed", lambda *_args: None)
    monkeypatch.setattr(
        embeddings,
        "upsert_after_write_status",
        lambda *_args: embeddings.EmbeddingSyncStatus(
            "completed", "embedding_upsert_completed", 1
        ),
    )
    monkeypatch.setattr(index_sync, "purge_semantic_only", lambda *_args: False)

    report = index_sync.upsert_after_write(tmp_path, [page])

    purge = next(item for item in report.components if item.component == "semantic_purge")
    assert (purge.outcome, purge.code) == ("degraded", "purge_failed")
    assert report.reconcile_required


def test_delete_after_move_purges_claim_and_semantic_receipt_but_keeps_identity_cleanup(
    tmp_path: Path, monkeypatch
) -> None:
    from exomem import claims, embeddings, epistemic_graph, find, lexstore, memory_refs

    old = tmp_path / "Knowledge Base" / "Notes" / "old.md"
    moved = tmp_path / "Knowledge Base" / "Notes" / "moved.md"
    old.parent.mkdir(parents=True)
    old.write_text("old", encoding="utf-8")
    old_rel = old.relative_to(tmp_path).as_posix()
    [receipt] = deferred_index.add_receipts(tmp_path, [old_rel])
    with sqlite3.connect(claims.sidecar_path(tmp_path)) as conn:
        conn.execute(
            "CREATE TABLE claims("
            "file_path TEXT PRIMARY KEY, claim_text TEXT, checksum TEXT, vector BLOB, "
            "page_type TEXT, status TEXT, file_mtime REAL)"
        )
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value INTEGER)")
        conn.execute(
            "INSERT INTO claims VALUES (?, 'stale claim', 'hash', X'00', 'insight', 'draft', 1)",
            (old_rel,),
        )
        conn.commit()
    old.replace(moved)
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        memory_refs,
        "delete_after_remove",
        lambda _root, paths: calls.append(("memory_refs", paths)),
    )
    monkeypatch.setattr(
        find,
        "on_resolver_files_changed",
        lambda _root, _changed, paths: calls.append(("resolver", paths)),
    )
    monkeypatch.setattr(lexstore, "delete_after_remove", lambda *_args: None)
    monkeypatch.setattr(
        epistemic_graph,
        "upsert_after_write",
        lambda *_args: epistemic_graph.GraphDispatchResult("completed", "incremental_completed"),
    )
    monkeypatch.setattr(
        embeddings,
        "delete_after_remove_status",
        lambda *_args: embeddings.EmbeddingSyncStatus("completed", "embedding_delete_completed", 1),
    )
    monkeypatch.setattr(
        embeddings,
        "delete_clip_after_remove",
        lambda *_args: embeddings.EmbeddingSyncStatus("completed", "clip_delete_completed", 1),
    )

    report = index_sync.delete_after_remove(tmp_path, [old_rel])

    with sqlite3.connect(claims.sidecar_path(tmp_path)) as conn:
        assert conn.execute("SELECT file_path FROM claims").fetchall() == []
    assert deferred_index.snapshot(tmp_path) == []
    assert calls == [("memory_refs", [old_rel]), ("resolver", [old_rel])]
    assert receipt.rel_path == old_rel
    assert next(item for item in report.components if item.component == "claims").outcome == "completed"
    assert next(
        item for item in report.components if item.component == "semantic_purge"
    ).outcome == "completed"


def test_removed_path_receipt_cleanup_cannot_clear_a_newer_recreate(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "Knowledge Base" / "Notes" / "race.md"
    path.parent.mkdir(parents=True)
    path.write_text("first", encoding="utf-8")
    rel = path.relative_to(tmp_path).as_posix()
    [old] = deferred_index.add_receipts(tmp_path, [rel])
    path.unlink()
    real_clear = deferred_index.clear_receipts

    def recreate_before_cas(vault_root: Path, receipts):
        path.write_text("recreated", encoding="utf-8")
        newer = deferred_index.add_receipts(vault_root, [rel])
        assert newer == [deferred_index.DeferredReceipt(rel, old.revision + 1)]
        return real_clear(vault_root, receipts)

    monkeypatch.setattr(deferred_index, "clear_receipts", recreate_before_cas)

    deferred_index.clear_semantic_receipts(tmp_path, [rel])

    assert deferred_index.snapshot(tmp_path) == [
        deferred_index.DeferredReceipt(rel, old.revision + 1)
    ]
