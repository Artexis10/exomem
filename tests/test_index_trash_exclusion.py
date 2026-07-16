"""Excluded scan dirs must stay excluded on the INCREMENTAL index paths.

Every full rebuild (walk_vault_md, find's walker, the inbound scan) skips
`VAULT_SCAN_SKIP_DIRS` (`_trash/`, `_archive/`, `_Schema/`, …). The
event-driven patch paths did not: `delete_file` moves a note into
`Knowledge Base/_trash/`, the watcher sees a fresh .md file there, and the
trashed content was re-embedded under its trash path — invisible to find()
but not to the corpus-aware near-dup sweep, which reads the raw sidecar
(observed live 2026-07-04, dup warnings pointing at `_trash/` entries).

The fix is two chokepoints sharing one predicate (`vault.in_excluded_scan_dir`):
the watcher drops excluded paths at its single event intake (`_record`), and
`index_sync.upsert_after_write` filters as the belt for direct writer calls.
Deletes stay UNfiltered so legacy pollution can still be purged.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from exomem import deferred_index, find_corpus, index_sync
from exomem.vault import in_excluded_scan_dir, walk_vault_md


def test_excluded_scan_dir_predicate() -> None:
    kb = "Knowledge Base"
    assert in_excluded_scan_dir(f"{kb}/_trash/2026-07-04/foo.md")
    assert in_excluded_scan_dir(f"{kb}/_archive/old.md")
    assert in_excluded_scan_dir(f"{kb}/_Schema/SKILL.md")
    assert in_excluded_scan_dir(f"{kb}/.graph-coordination/mutation-locks/lock.md")
    assert in_excluded_scan_dir(".obsidian/workspace.json")
    # Backslash tolerance (Windows callers).
    assert in_excluded_scan_dir(f"{kb}\\_trash\\2026-07-04\\foo.md")
    # Normal content is NOT excluded.
    assert not in_excluded_scan_dir(f"{kb}/Notes/Insights/foo.md")
    assert not in_excluded_scan_dir(f"{kb}/Sources/Articles/bar.md")
    # Only whole segments match — a note ABOUT trash isn't excluded.
    assert not in_excluded_scan_dir(f"{kb}/Notes/Insights/_trash-handling.md")


def test_graph_coordination_directory_is_excluded_from_both_full_walkers(
    vault: Path,
) -> None:
    kb = vault / "Knowledge Base"
    coordination_note = kb / ".graph-coordination" / "unreadable-lock-state.md"
    normal_note = kb / "Notes" / "normal.md"
    coordination_note.parent.mkdir(parents=True, exist_ok=True)
    normal_note.parent.mkdir(parents=True, exist_ok=True)
    coordination_note.write_text("# coordination state\n", encoding="utf-8")
    normal_note.write_text("# normal\n", encoding="utf-8")

    assert normal_note in set(find_corpus.walk_md(kb))
    assert normal_note in set(walk_vault_md(vault))
    assert coordination_note not in set(find_corpus.walk_md(kb))
    assert coordination_note not in set(walk_vault_md(vault))


def test_index_sync_upsert_drops_excluded_paths(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, list[Path]] = {}

    def _rec(name):
        def hook(vault_root, paths):
            seen[name] = list(paths)
        return hook

    from exomem import embeddings, find, lexstore

    monkeypatch.setattr(lexstore, "upsert_after_write", _rec("lexstore"))
    monkeypatch.setattr(
        embeddings,
        "upsert_after_write_status",
        lambda root, paths: _rec("embeddings")(root, paths)
        or embeddings.EmbeddingSyncStatus(
            "completed", "embedding_upsert_completed", len(paths)
        ),
    )
    resolver_rels: list[str] = []
    monkeypatch.setattr(
        find, "on_resolver_files_changed",
        lambda vr, changed, deleted: resolver_rels.extend(changed),
    )

    good = vault / "Knowledge Base" / "Notes" / "Insights" / "keep-me.md"
    trashed = vault / "Knowledge Base" / "_trash" / "2026-07-04" / "gone.md"
    trashed.parent.mkdir(parents=True, exist_ok=True)
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_text("# keep\n", encoding="utf-8")
    trashed.write_text("# gone\n", encoding="utf-8")

    index_sync.upsert_after_write(vault, [good, trashed])

    assert seen["lexstore"] == [good]
    assert seen["embeddings"] == [good]
    assert resolver_rels == ["Knowledge Base/Notes/Insights/keep-me.md"]


def test_index_sync_upsert_noop_when_all_excluded(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import embeddings, lexstore

    called: list[str] = []
    monkeypatch.setattr(
        lexstore, "upsert_after_write", lambda vr, p: called.append("lex")
    )
    monkeypatch.setattr(
        embeddings, "upsert_after_write", lambda vr, p: called.append("emb")
    )
    trashed = vault / "Knowledge Base" / "_trash" / "2026-07-04" / "gone.md"
    trashed.parent.mkdir(parents=True, exist_ok=True)
    trashed.write_text("# gone\n", encoding="utf-8")

    index_sync.upsert_after_write(vault, [trashed])

    assert called == []


def test_index_sync_quiet_defers_semantic_upserts(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "quiet")
    index_sync.clear_deferred_work(vault)
    from exomem import embeddings, find, lexstore

    seen: dict[str, list] = {"lexstore": [], "embeddings": [], "resolver": []}
    monkeypatch.setattr(
        lexstore,
        "upsert_after_write",
        lambda root, paths: seen["lexstore"].append(list(paths)),
    )
    monkeypatch.setattr(
        embeddings,
        "upsert_after_write_status",
        lambda root, paths: seen["embeddings"].append(list(paths))
        or embeddings.EmbeddingSyncStatus(
            "completed", "embedding_upsert_completed", len(paths)
        ),
    )
    monkeypatch.setattr(
        find,
        "on_resolver_files_changed",
        lambda root, changed, deleted: seen["resolver"].append(
            (list(changed), list(deleted))
        ),
    )

    good = vault / "Knowledge Base" / "Notes" / "Insights" / "quiet-defers.md"
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_text("# quiet\n", encoding="utf-8")

    try:
        index_sync.upsert_after_write(vault, [good, good])

        assert seen["lexstore"] == [[good, good]]
        assert seen["embeddings"] == []
        assert seen["resolver"] == [
            (
                [
                    "Knowledge Base/Notes/Insights/quiet-defers.md",
                    "Knowledge Base/Notes/Insights/quiet-defers.md",
                ],
                [],
            )
        ]
        status = index_sync.deferred_work_status(vault)["semantic_upserts"]
        assert status["count"] == 1
        assert status["paths"] == ["Knowledge Base/Notes/Insights/quiet-defers.md"]
    finally:
        index_sync.clear_deferred_work(vault)


def test_index_sync_explicit_defer_semantic_upserts(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "normal")
    index_sync.clear_deferred_work(vault)
    from exomem import embeddings, find, lexstore

    seen: dict[str, list] = {"lexstore": [], "embeddings": [], "resolver": []}
    monkeypatch.setattr(
        lexstore,
        "upsert_after_write",
        lambda root, paths: seen["lexstore"].append(list(paths)),
    )
    monkeypatch.setattr(
        embeddings,
        "upsert_after_write",
        lambda root, paths: seen["embeddings"].append(list(paths)),
    )
    monkeypatch.setattr(
        find,
        "on_resolver_files_changed",
        lambda root, changed, deleted: seen["resolver"].append(
            (list(changed), list(deleted))
        ),
    )

    good = vault / "Knowledge Base" / "Notes" / "Insights" / "defer-explicit.md"
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_text("# defer\n", encoding="utf-8")

    try:
        index_sync.upsert_after_write(vault, [good], defer_semantic=True)

        assert seen["lexstore"] == [[good]]
        assert seen["embeddings"] == []
        assert seen["resolver"] == [(["Knowledge Base/Notes/Insights/defer-explicit.md"], [])]
        status = index_sync.deferred_work_status(vault)["semantic_upserts"]
        assert status["count"] == 1
        assert status["paths"] == ["Knowledge Base/Notes/Insights/defer-explicit.md"]
    finally:
        index_sync.clear_deferred_work(vault)


def test_bulk_defer_skips_only_proven_current_without_clearing_existing_receipt(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "normal")
    from exomem import epistemic_graph, find, lexstore, memory_refs

    for module in (lexstore, memory_refs, epistemic_graph):
        monkeypatch.setattr(module, "upsert_after_write", lambda *_a, **_kw: None)
    monkeypatch.setattr(find, "on_resolver_files_changed", lambda *_a, **_kw: None)
    root = vault / "Knowledge Base" / "Notes" / "Insights"
    root.mkdir(parents=True, exist_ok=True)
    current = root / "current.md"
    current_with_receipt = root / "current-receipt.md"
    stale = root / "stale.md"
    for path in (current, current_with_receipt, stale):
        path.write_text("# note\n", encoding="utf-8")
    sidecar = vault / "Knowledge Base" / ".embeddings.sqlite"
    conn = sqlite3.connect(sidecar)
    try:
        conn.execute(
            "CREATE TABLE chunks (file_path TEXT, chunk_idx INTEGER, file_mtime REAL)"
        )
        conn.executemany(
            "INSERT INTO chunks VALUES (?, 0, ?)",
            [
                (path.relative_to(vault).as_posix(), path.stat().st_mtime)
                for path in (current, current_with_receipt)
            ],
        )
        conn.commit()
    finally:
        conn.close()
    receipt_rel = current_with_receipt.relative_to(vault).as_posix()
    [existing] = deferred_index.add_receipts(vault, [receipt_rel])

    index_sync.upsert_after_write(
        vault, [current, current_with_receipt, stale], defer_semantic=True
    )

    receipts = {item.rel_path: item for item in deferred_index.snapshot(vault)}
    assert current.relative_to(vault).as_posix() not in receipts
    assert receipts[receipt_rel] == existing
    assert stale.relative_to(vault).as_posix() in receipts


@pytest.mark.parametrize(
    ("status", "code", "cleared"),
    [
        ("completed", "embedding_upsert_completed", True),
        ("disabled", "embeddings_disabled", False),
        ("deferred", "deferred_warmup", False),
        ("degraded", "embedding_upsert_failed", False),
        ("degraded", "embedding_auxiliary_failed", False),
    ],
)
def test_replay_clears_only_completed_receipt(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    code: str,
    cleared: bool,
) -> None:
    from exomem import embeddings

    path = vault / "Knowledge Base" / "Notes" / "replay.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# replay\n", encoding="utf-8")
    [receipt] = deferred_index.add_receipts(
        vault, [path.relative_to(vault).as_posix()]
    )
    calls: list[bool] = []

    def replay_status(_vault, _paths, *, defer_during_warm=True):
        calls.append(defer_during_warm)
        return embeddings.EmbeddingSyncStatus(status, code, 1)

    monkeypatch.setattr(embeddings, "upsert_after_write_status", replay_status)

    result = index_sync.replay_deferred_embedding(vault, [path], [receipt])

    assert result.status == status
    assert calls == [False]
    assert (deferred_index.snapshot(vault) == []) is cleared


def test_old_replay_cannot_clear_newer_same_path_receipt(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import embeddings

    path = vault / "Knowledge Base" / "Notes" / "race.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# race\n", encoding="utf-8")
    rel = path.relative_to(vault).as_posix()
    [old] = deferred_index.add_receipts(vault, [rel])
    newer: list[deferred_index.DeferredReceipt] = []

    def replay_status(*_args, **_kwargs):
        newer.extend(deferred_index.add_receipts(vault, [rel]))
        return embeddings.EmbeddingSyncStatus(
            "completed", "embedding_upsert_completed", 1
        )

    monkeypatch.setattr(embeddings, "upsert_after_write_status", replay_status)

    index_sync.replay_deferred_embedding(vault, [path], [old])

    assert deferred_index.snapshot(vault) == newer



def test_replay_exception_preserves_receipt(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import embeddings

    path = vault / "Knowledge Base" / "Notes" / "exception.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# exception\n", encoding="utf-8")
    [receipt] = deferred_index.add_receipts(
        vault, [path.relative_to(vault).as_posix()]
    )
    monkeypatch.setattr(
        embeddings,
        "upsert_after_write_status",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    with pytest.raises(RuntimeError, match="boom"):
        index_sync.replay_deferred_embedding(vault, [path], [receipt])

    assert deferred_index.snapshot(vault) == [receipt]


def test_index_sync_nonquiet_keeps_immediate_embedding_upsert(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "normal")
    index_sync.clear_deferred_work(vault)
    from exomem import embeddings, find, lexstore

    seen: dict[str, list] = {"lexstore": [], "embeddings": [], "resolver": []}
    monkeypatch.setattr(
        lexstore,
        "upsert_after_write",
        lambda root, paths: seen["lexstore"].append(list(paths)),
    )
    monkeypatch.setattr(
        embeddings,
        "upsert_after_write_status",
        lambda root, paths: seen["embeddings"].append(list(paths))
        or embeddings.EmbeddingSyncStatus(
            "completed", "embedding_upsert_completed", len(paths)
        ),
    )
    monkeypatch.setattr(
        find,
        "on_resolver_files_changed",
        lambda root, changed, deleted: seen["resolver"].append(
            (list(changed), list(deleted))
        ),
    )

    good = vault / "Knowledge Base" / "Notes" / "Insights" / "normal-upserts.md"
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_text("# normal\n", encoding="utf-8")

    index_sync.upsert_after_write(vault, [good])

    assert seen["lexstore"] == [[good]]
    assert seen["embeddings"] == [[good]]
    assert seen["resolver"] == [(["Knowledge Base/Notes/Insights/normal-upserts.md"], [])]
    assert index_sync.deferred_work_status(vault)["semantic_upserts"]["count"] == 0


def test_drain_deferred_work_processes_and_clears_semantic_upserts(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "quiet")
    index_sync.clear_deferred_work(vault)
    from exomem import embeddings, find, lexstore

    monkeypatch.setattr(lexstore, "upsert_after_write", lambda root, paths: None)
    monkeypatch.setattr(
        find,
        "on_resolver_files_changed",
        lambda root, changed, deleted: None,
    )
    calls: list[list[Path]] = []

    def completed(root, paths, *, defer_during_warm=True):
        assert defer_during_warm is False
        calls.append(list(paths))
        return embeddings.EmbeddingSyncStatus("completed", "test", len(paths))

    monkeypatch.setattr(
        embeddings,
        "upsert_after_write_status",
        completed,
    )
    good = vault / "Knowledge Base" / "Notes" / "Insights" / "drain-me.md"
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_text("# drain\n", encoding="utf-8")

    index_sync.upsert_after_write(vault, [good])
    assert index_sync.deferred_work_status(vault)["semantic_upserts"]["count"] == 1

    processed = index_sync.drain_deferred_work(vault)

    assert processed == 1
    assert calls == [[good]]
    assert index_sync.deferred_work_status(vault)["semantic_upserts"]["count"] == 0


def test_drain_legacy_receipt_registry_before_any_writable_migration(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import embeddings

    rel = "Knowledge Base/Notes/Insights/legacy-drain.md"
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# legacy drain\n", encoding="utf-8")
    registry = deferred_index.store_path(vault)
    registry.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(registry)
    try:
        conn.execute(
            "CREATE TABLE semantic_upserts ("
            "rel_path TEXT PRIMARY KEY, created_at REAL NOT NULL, "
            "updated_at REAL NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE full_upserts ("
            "rel_path TEXT PRIMARY KEY, created_at REAL NOT NULL, "
            "updated_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO semantic_upserts VALUES (?, 1, 1)",
            (rel,),
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(
        embeddings,
        "upsert_after_write_status",
        lambda *_args, **_kwargs: embeddings.EmbeddingSyncStatus(
            "completed", "embedding_upsert_completed", 1
        ),
    )

    assert index_sync.drain_deferred_work(vault) == 1
    assert deferred_index.snapshot(vault) == []


def test_drain_deferred_work_preserves_failed_semantic_upserts(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "quiet")
    index_sync.clear_deferred_work(vault)
    from exomem import embeddings, find, lexstore

    monkeypatch.setattr(lexstore, "upsert_after_write", lambda root, paths: None)
    monkeypatch.setattr(find, "on_resolver_files_changed", lambda root, changed, deleted: None)
    monkeypatch.setattr(embeddings, "upsert_after_write", lambda root, paths: False)
    good = vault / "Knowledge Base" / "Notes" / "Insights" / "retry-me.md"
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_text("# retry\n", encoding="utf-8")

    index_sync.upsert_after_write(vault, [good])
    assert index_sync.drain_deferred_work(vault) == 0
    assert index_sync.deferred_work_status(vault)["semantic_upserts"]["count"] == 1


def test_embedding_only_clear_preserves_full_index_retry(vault: Path) -> None:
    rel = "Knowledge Base/Evidence/Audio/preserve-full-retry.m4a.md"
    deferred_index.add(vault, [rel])
    deferred_index.add_full(vault, [rel])

    assert index_sync.clear_deferred_work(vault) == 1
    assert deferred_index.status(vault)["count"] == 0
    assert deferred_index.full_status(vault)["paths"] == [rel]


def test_full_index_drain_keeps_work_when_embeddings_report_incomplete(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "normal")
    from exomem import embeddings, epistemic_graph, find, lexstore, memory_refs

    target = vault / "Knowledge Base" / "Notes" / "Insights" / "full-retry.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# full retry\n", encoding="utf-8")
    deferred_index.add_full(vault, [target.relative_to(vault).as_posix()])
    monkeypatch.setattr(lexstore, "upsert_after_write", lambda *_a, **_kw: None)
    monkeypatch.setattr(memory_refs, "upsert_after_write", lambda *_a, **_kw: None)
    monkeypatch.setattr(find, "on_resolver_files_changed", lambda *_a, **_kw: None)
    monkeypatch.setattr(epistemic_graph, "upsert_after_write", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        embeddings,
        "upsert_after_write_status",
        lambda *_a, **_kw: embeddings.EmbeddingSyncStatus(
            "degraded", "embedding_upsert_failed", 1
        ),
    )

    assert index_sync.drain_deferred_work(vault) == 0
    assert deferred_index.full_status(vault)["paths"] == [
        target.relative_to(vault).as_posix()
    ]

    monkeypatch.setattr(
        embeddings,
        "upsert_after_write_status",
        lambda *_a, **_kw: embeddings.EmbeddingSyncStatus(
            "completed", "embedding_upsert_completed", 1
        ),
    )
    assert index_sync.drain_deferred_work(vault) == 2
    assert deferred_index.full_status(vault)["count"] == 0
    assert deferred_index.status(vault)["count"] == 0


def test_full_index_drain_can_target_one_sidecar(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = vault / "Knowledge Base" / "Evidence" / "Audio" / "first.m4a.md"
    second = vault / "Knowledge Base" / "Evidence" / "Audio" / "second.m4a.md"
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_text("# first\n", encoding="utf-8")
    second.write_text("# second\n", encoding="utf-8")
    deferred_index.add_full(
        vault,
        [first.relative_to(vault).as_posix(), second.relative_to(vault).as_posix()],
    )
    monkeypatch.setattr(index_sync, "upsert_after_write", lambda *_a, **_kw: True)

    assert index_sync.drain_deferred_work(vault, paths=[first]) == 1
    assert deferred_index.full_status(vault)["paths"] == [
        second.relative_to(vault).as_posix()
    ]
