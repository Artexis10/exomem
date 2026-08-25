from __future__ import annotations

import math
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import deferred_index, doctor, file_watcher, index_sync, mode, mutation_lock
from exomem.__main__ import _index_main, _mode_main


def _completed_full_upsert_report(paths: list[Path]) -> index_sync.IndexSyncReport:
    rels = tuple(
        Path(*path.parts[next(i for i, part in enumerate(path.parts) if part == "Knowledge Base") :]).as_posix()
        for path in paths
    )
    return index_sync.IndexSyncReport(
        "upsert",
        rels,
        rels,
        tuple(
            index_sync.IndexComponentOutcome(component, "completed", "completed")
            for component in (
                "memory_refs",
                "resolver",
                "semantic_purge",
                "lexstore",
                "epistemic_graph",
                "embeddings",
            )
        ),
    )


def test_add_full_receipts_returns_its_atomic_revision_when_a_readd_races(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full receipt's return value cannot be reconstructed from a later snapshot."""
    rels = ["Knowledge Base/racing-receipt.md", "Knowledge Base/racing-other.md"]
    for rel in rels:
        target = vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {target.stem}\n", encoding="utf-8")
    deferred_index.add_full(vault, rels)
    original_connect = deferred_index._connect
    main_thread = threading.current_thread()
    race_started = threading.Event()
    race_finished = threading.Event()
    race_thread: threading.Thread | None = None

    def concurrent_readd() -> None:
        deferred_index.add_full(vault, [rels[0]])
        race_finished.set()

    def release_competitor_after_commit() -> None:
        nonlocal race_thread
        if race_started.is_set():
            return
        race_started.set()
        race_thread = threading.Thread(target=concurrent_readd)
        race_thread.start()
        assert race_finished.wait(timeout=5)

    class _ConnectionProxy:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def __enter__(self):
            self._connection.__enter__()
            return self

        def __exit__(self, *args):  # noqa: ANN002
            result = self._connection.__exit__(*args)
            release_competitor_after_commit()
            return result

        def __getattr__(self, name: str):
            return getattr(self._connection, name)

        def commit(self) -> None:
            self._connection.commit()
            release_competitor_after_commit()

    def connect(*args, **kwargs):  # noqa: ANN002, ANN003
        connection = original_connect(*args, **kwargs)
        if threading.current_thread() is main_thread:
            return _ConnectionProxy(connection)
        return connection

    monkeypatch.setattr(deferred_index, "_connect", connect)

    admitted = deferred_index.add_full_receipts(vault, rels)

    assert race_started.is_set()
    assert race_thread is not None
    race_thread.join(timeout=5)
    assert race_finished.is_set()
    assert admitted == [
        deferred_index.DeferredReceipt(rels[1], 2),
        deferred_index.DeferredReceipt(rels[0], 2),
    ]
    assert deferred_index.snapshot_full(vault) == [
        deferred_index.DeferredReceipt(rels[1], 2),
        deferred_index.DeferredReceipt(rels[0], 3),
    ]


def test_index_command_clears_both_deferred_queues(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import accel, embeddings

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(accel, "bulk_device", lambda _device: "cpu")
    monkeypatch.setattr(accel, "select_device", lambda **_kwargs: "cpu")
    monkeypatch.setattr(embeddings, "get_model", lambda: object())
    monkeypatch.setattr(
        embeddings,
        "index_incremental",
        lambda *_args, **_kwargs: {"files_to_embed": 0},
    )
    calls: list[tuple[Path, list[Path] | None, bool]] = []
    drained: list[Path] = []
    monkeypatch.setattr(
        index_sync,
        "drain_deferred_work",
        lambda root: drained.append(root) or 0,
    )
    monkeypatch.setattr(
        index_sync,
        "clear_deferred_work",
        lambda root, *, paths=None, include_full=False: calls.append(
            (root, paths, include_full)
        )
        or 0,
    )

    assert _index_main(["--vault", str(vault), "--device", "cpu"]) == 0
    assert drained == [vault]
    assert calls == [(vault, [], True)]


def test_drain_retires_current_entries_and_performs_stale_work_first(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current_rel = "Knowledge Base/current.md"
    stale_rel = "Knowledge Base/stale.md"
    for rel in (current_rel, stale_rel):
        target = vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {rel}\n", encoding="utf-8")
    deferred_index.add(vault, [current_rel, stale_rel])
    monkeypatch.setattr(
        deferred_index,
        "inspect_embedding_freshness",
        lambda _root, _rels, **_kwargs: {
            current_rel: deferred_index.EmbeddingFreshness.CURRENT,
            stale_rel: deferred_index.EmbeddingFreshness.STALE,
        },
    )
    replayed: list[list[str]] = []

    def replay(root: Path, paths: list[Path], receipts):  # noqa: ANN001
        replayed.append([path.relative_to(root).as_posix() for path in paths])
        assert deferred_index.status(root)["count"] == 1
        assert [receipt.rel_path for receipt in receipts] == [stale_rel]
        deferred_index.clear_receipts(root, list(receipts))
        return SimpleNamespace(status="completed")

    monkeypatch.setattr(index_sync, "replay_deferred_embedding", replay)

    assert index_sync.drain_deferred_work(vault) == 2
    assert replayed == [[stale_rel]]
    assert deferred_index.status(vault)["count"] == 0


def test_freshness_requires_semantic_unit_parity_not_only_current_chunks(
    vault: Path,
) -> None:
    from exomem import find as find_module
    from exomem import index_paths

    rel = "Knowledge Base/unit-parity.md"
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\ntype: insight\ntitle: Unit parity\n"
        "exomem_id: 11111111-1111-4111-8111-111111111111\n---\n"
        "# Unit parity\n\n- [rule] keep the unit vector current ^unit\n",
        encoding="utf-8",
    )
    find_module.clear_cache()
    sidecar = index_paths.sidecar_path(vault)
    with sqlite3.connect(sidecar) as connection:
        connection.execute(
            "CREATE TABLE chunks (file_path TEXT, chunk_idx INTEGER, file_mtime REAL)"
        )
        connection.execute(
            "CREATE TABLE semantic_unit_vectors ("
            "parent_path TEXT, parent_generation TEXT, unit_ref TEXT)"
        )
        connection.execute(
            "INSERT INTO chunks(file_path, chunk_idx, file_mtime) VALUES (?, 0, ?)",
            (rel, target.stat().st_mtime),
        )

    result = deferred_index.inspect_embedding_freshness(
        vault, [rel], mtime_slack_seconds=1.0
    )

    assert result[rel] is deferred_index.EmbeddingFreshness.STALE


def test_freshness_requires_obsolete_chunk_rows_to_be_pruned(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import embeddings, index_paths
    from exomem import find as find_module

    rel = "Knowledge Base/no-longer-chunked.md"
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# no longer chunked\n", encoding="utf-8")
    find_module.clear_cache()
    sidecar = index_paths.sidecar_path(vault)
    with sqlite3.connect(sidecar) as connection:
        connection.execute(
            "CREATE TABLE chunks (file_path TEXT, chunk_idx INTEGER, file_mtime REAL)"
        )
        connection.execute(
            "INSERT INTO chunks(file_path, chunk_idx, file_mtime) VALUES (?, 0, ?)",
            (rel, target.stat().st_mtime),
        )
    monkeypatch.setattr(embeddings, "_chunks_for_page", lambda *_args: [])

    result = deferred_index.inspect_embedding_freshness(
        vault, [rel], mtime_slack_seconds=1.0
    )

    assert result[rel] is deferred_index.EmbeddingFreshness.STALE


def test_full_drain_preserves_work_requeued_during_dispatch(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rel = "Knowledge Base/requeued.md"
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# requeued\n", encoding="utf-8")
    deferred_index.add_full(vault, [rel])

    def dispatch(root: Path, paths: list[Path]):
        assert paths == [target]
        deferred_index.add_full(root, [rel])
        return _completed_full_upsert_report(paths)

    monkeypatch.setattr(index_sync, "upsert_after_write", dispatch)

    assert index_sync.drain_deferred_work(vault) == 0
    [remaining] = deferred_index.snapshot_full(vault)
    assert remaining.rel_path == rel
    assert remaining.revision == 2


def test_targeted_full_drain_reads_only_the_bounded_requested_receipts(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Media-targeted replay must not materialize either complete backlog."""
    rels = [f"Knowledge Base/backlog-{index:02d}.md" for index in range(24)]
    for rel in rels:
        target = vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# queued\n", encoding="utf-8")
    deferred_index.add_full(vault, rels)
    snapshots: list[tuple[int | None, set[str] | None]] = []
    real_snapshot_full = deferred_index.snapshot_full

    def observe_snapshot_full(root: Path, *, limit=None, paths=None):  # noqa: ANN001
        snapshots.append((limit, paths))
        return real_snapshot_full(root, limit=limit, paths=paths)

    dispatched: list[list[Path]] = []
    monkeypatch.setattr(deferred_index, "snapshot_full", observe_snapshot_full)
    monkeypatch.setattr(
        index_sync,
        "upsert_after_write",
        lambda _root, paths: dispatched.append(paths) or _completed_full_upsert_report(paths),
    )

    assert index_sync.drain_deferred_work(
        vault,
        paths=[vault / rels[3], vault / rels[7]],
        limit=1,
    ) == 1
    assert snapshots == [(1, {rels[3], rels[7]})]
    assert dispatched == [[vault / rels[3]]]


def test_legacy_full_queue_migrates_to_revisioned_receipts(vault: Path) -> None:
    rel = "Knowledge Base/legacy-full.md"
    store = deferred_index.store_path(vault)
    store.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(store) as connection:
        connection.execute(
            "CREATE TABLE full_upserts ("
            "rel_path TEXT PRIMARY KEY, created_at REAL NOT NULL, "
            "updated_at REAL NOT NULL)"
        )
        connection.execute(
            "INSERT INTO full_upserts(rel_path, created_at, updated_at) "
            "VALUES (?, 1, 1)",
            (rel,),
        )

    [legacy] = deferred_index.snapshot_full(vault)
    assert legacy.revision == 1
    deferred_index.add_full(vault, [rel])
    [requeued] = deferred_index.snapshot_full(vault)
    assert requeued.revision == 2
    assert deferred_index.clear_full_receipts(vault, [legacy]) == 0


def test_unbounded_manual_drain_reconciles_semantic_work_created_by_full_refresh(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rel = "Knowledge Base/full-then-semantic.md"
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# full then semantic\n", encoding="utf-8")
    deferred_index.add_full(vault, [rel])

    def dispatch(root: Path, paths: list[Path]):
        assert paths == [target]
        deferred_index.add(root, [rel])
        return _completed_full_upsert_report(paths)

    monkeypatch.setattr(index_sync, "upsert_after_write", dispatch)
    monkeypatch.setattr(
        deferred_index,
        "inspect_embedding_freshness",
        lambda _root, rels, **_kwargs: {
            queued: deferred_index.EmbeddingFreshness.CURRENT for queued in rels
        },
    )

    assert index_sync.drain_deferred_work(vault) == 2
    assert deferred_index.full_status(vault)["count"] == 0
    assert deferred_index.status(vault)["count"] == 0


def test_full_drain_isolates_a_failed_receipt_from_later_work(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad_rel = "Knowledge Base/a-bad.md"
    good_rel = "Knowledge Base/z-good.md"
    for rel in (bad_rel, good_rel):
        target = vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {rel}\n", encoding="utf-8")
    deferred_index.add_full(vault, [bad_rel, good_rel])
    dispatched: list[str] = []

    def dispatch(_root: Path, paths: list[Path]):
        assert len(paths) == 1
        rel = paths[0].relative_to(vault).as_posix()
        dispatched.append(rel)
        outcomes = (
            (index_sync.IndexComponentOutcome("lexstore", "degraded", "failed"),)
            if rel == bad_rel
            else ()
        )
        return (
            index_sync.IndexSyncReport("upsert", (rel,), (rel,), outcomes)
            if outcomes
            else _completed_full_upsert_report(paths)
        )

    monkeypatch.setattr(index_sync, "upsert_after_write", dispatch)

    assert index_sync.drain_deferred_work(vault) == 1
    assert dispatched == [bad_rel, good_rel]
    assert [receipt.rel_path for receipt in deferred_index.snapshot_full(vault)] == [
        bad_rel
    ]


def test_failed_full_prefix_rotates_so_later_work_is_not_starved(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed = [f"Knowledge Base/a-failed-{index:02d}.md" for index in range(14)]
    good_rel = "Knowledge Base/z-good.md"
    for rel in (*failed, good_rel):
        target = vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {rel}\n", encoding="utf-8")
    deferred_index.add_full(vault, [*failed, good_rel])

    def dispatch(_root: Path, paths: list[Path]):
        rels = [path.relative_to(vault).as_posix() for path in paths]
        if len(rels) > 1 or any(rel in failed for rel in rels):
            return index_sync.IndexSyncReport(
                "upsert",
                tuple(rels),
                tuple(rels),
                (index_sync.IndexComponentOutcome("lexstore", "degraded", "failed"),),
            )
        return _completed_full_upsert_report(paths)

    monkeypatch.setattr(index_sync, "upsert_after_write", dispatch)

    outcomes = [index_sync.drain_deferred_work(vault, limit=12) for _ in range(4)]
    assert outcomes == [0, 0, 0, 1]
    assert good_rel not in deferred_index.list_full_paths(vault)


def test_semantic_drain_isolates_an_incomplete_receipt_from_later_work(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad_rel = "Knowledge Base/a-bad-semantic.md"
    good_rel = "Knowledge Base/z-good-semantic.md"
    for rel in (bad_rel, good_rel):
        target = vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {rel}\n", encoding="utf-8")
    deferred_index.add(vault, [bad_rel, good_rel])
    monkeypatch.setattr(
        deferred_index,
        "inspect_embedding_freshness",
        lambda _root, rels, **_kwargs: {
            rel: deferred_index.EmbeddingFreshness.STALE for rel in rels
        },
    )
    attempts: list[list[str]] = []

    def replay(root: Path, _paths: list[Path], receipts):  # noqa: ANN001
        rels = [receipt.rel_path for receipt in receipts]
        attempts.append(rels)
        if rels == [good_rel]:
            deferred_index.clear_receipts(root, list(receipts))
            return SimpleNamespace(status="completed")
        return SimpleNamespace(status="degraded")

    monkeypatch.setattr(index_sync, "replay_deferred_embedding", replay)

    assert index_sync.drain_deferred_work(vault) == 1
    assert attempts == [[bad_rel, good_rel], [bad_rel], [good_rel]]
    assert [receipt.rel_path for receipt in deferred_index.snapshot(vault)] == [
        bad_rel
    ]


def test_failed_semantic_prefix_rotates_so_later_work_is_not_starved(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed = [f"Knowledge Base/a-failed-semantic-{index:02d}.md" for index in range(14)]
    good_rel = "Knowledge Base/z-good-semantic.md"
    for rel in (*failed, good_rel):
        target = vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {rel}\n", encoding="utf-8")
    deferred_index.add(vault, [*failed, good_rel])
    monkeypatch.setattr(
        deferred_index,
        "inspect_embedding_freshness",
        lambda _root, rels, **_kwargs: {
            rel: deferred_index.EmbeddingFreshness.STALE for rel in rels
        },
    )

    def replay(root: Path, _paths: list[Path], receipts):  # noqa: ANN001
        rels = [receipt.rel_path for receipt in receipts]
        if rels == [good_rel]:
            deferred_index.clear_receipts(root, list(receipts))
            return SimpleNamespace(status="completed")
        return SimpleNamespace(status="degraded")

    monkeypatch.setattr(index_sync, "replay_deferred_embedding", replay)

    outcomes = [index_sync.drain_deferred_work(vault, limit=12) for _ in range(4)]
    assert outcomes == [0, 0, 0, 1]
    assert good_rel not in deferred_index.list_paths(vault)


def test_quiet_policy_has_nonzero_bounded_deferred_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "quiet")
    policy = mode.watcher_policy()
    assert policy.max_embed_files_per_batch is not None
    assert 0 < policy.max_embed_files_per_batch <= 100
    assert policy.max_reconcile_embed_files == policy.max_embed_files_per_batch


def test_zero_live_cap_keeps_one_background_convergence_slot() -> None:
    policy = mode.WatcherPolicy(0.5, 300.0, 0, 500, False)

    assert file_watcher._background_deferred_limit(policy, 500) == 1
    assert file_watcher._background_deferred_limit(policy, 0) == 0


def test_zero_live_cap_periodic_drain_progresses_without_overspending_drift(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rel = "Knowledge Base/zero-cap.md"
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# queued\n", encoding="utf-8")
    monkeypatch.setenv("EXOMEM_MODE", "normal")
    monkeypatch.setenv("EXOMEM_WATCHER_MAX_EMBED_FILES", "0")
    assert mode.watcher_policy().max_embed_files_per_batch == 0
    monkeypatch.setattr(file_watcher.freshness, "SCOPES", ())
    monkeypatch.setattr(
        file_watcher.media_processing, "reconcile_all_media", lambda *_a, **_kw: 0
    )
    monkeypatch.setattr(
        file_watcher.freshness, "external_pending_epoch", lambda _root: None
    )
    monkeypatch.setattr(file_watcher.freshness, "external_pending", lambda _root: False)
    monkeypatch.setattr(
        file_watcher.FileWatcher, "_recover_suspended_graph", lambda _self: None
    )
    monkeypatch.setattr(
        deferred_index,
        "inspect_embedding_freshness",
        lambda _root, paths, **_kwargs: {
            queued: deferred_index.EmbeddingFreshness.STALE for queued in paths
        },
    )

    def replay(root: Path, _paths: list[Path], receipts):  # noqa: ANN001
        deferred_index.clear_receipts(root, list(receipts))
        return SimpleNamespace(status="completed")

    monkeypatch.setattr(index_sync, "replay_deferred_embedding", replay)
    watcher = file_watcher.FileWatcher(vault)
    deferred_index.add(vault, [rel])

    watcher._reconcile_once(seed=False)

    assert deferred_index.list_paths(vault) == []

    deferred_index.add(vault, [rel])
    monkeypatch.setattr(file_watcher.freshness, "SCOPES", ("kb",))
    monkeypatch.setattr(
        file_watcher.freshness,
        "reconcile",
        lambda *_a, **_kw: SimpleNamespace(drifted=True, changed=(), deleted=()),
    )
    monkeypatch.setattr(
        watcher,
        "_dispatch_reconcile_delta",
        lambda *_a: mode.watcher_policy().max_reconcile_embed_files,
    )
    monkeypatch.setattr("exomem.bm25.warm", lambda *_a: None)

    watcher._reconcile_once(seed=False)

    assert deferred_index.list_paths(vault) == [rel]


def test_quiet_watcher_sets_the_deferral_flag_it_logs(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import epistemic_graph, find, semantic_writes
    from exomem import vault as vault_module

    target = vault / "Knowledge Base" / "quiet.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# quiet\n", encoding="utf-8")
    deferred_index.add(vault, [target.relative_to(vault).as_posix()])
    monkeypatch.setattr(
        file_watcher.FileWatcher,
        "_watcher_policy",
        lambda _self: mode.WatcherPolicy(2.0, 900.0, 25, 25, True),
    )
    monkeypatch.setattr(vault_module, "on_inbound_files_changed", lambda *_a: None)
    monkeypatch.setattr(find, "on_resolver_files_changed", lambda *_a: None)
    monkeypatch.setattr(epistemic_graph, "graph_enabled", lambda: False)
    monkeypatch.setattr(
        semantic_writes,
        "evaluate_posthoc_batch",
        lambda *_a, **_kw: SimpleNamespace(
            as_dict=lambda: {"semantic_contract_findings": 0}
        ),
    )
    observed: list[bool] = []
    monkeypatch.setattr(
        index_sync,
        "upsert_after_write",
        lambda *_a, defer_semantic, **_kw: observed.append(defer_semantic),
    )
    monkeypatch.setattr(
        index_sync,
        "drain_deferred_work",
        lambda *_a, **_kw: pytest.fail("live write path must not drain queued work"),
    )

    file_watcher.FileWatcher(vault)._dispatch_batch(
        [target],
        [target.relative_to(vault).as_posix()],
        [],
        cap=False,
        publish_corpus_change=False,
    )

    assert observed == [True]
    assert deferred_index.status(vault)["count"] == 1


def test_quiet_reconcile_passes_converge_a_corpus_scale_backlog(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "quiet")
    cap = mode.watcher_policy().max_reconcile_embed_files
    assert cap is not None and cap > 0
    rels = [f"Knowledge Base/backlog/{index:04d}.md" for index in range(2800)]
    deferred_index.add(vault, rels)
    monkeypatch.setattr(file_watcher.freshness, "SCOPES", ())
    monkeypatch.setattr(
        file_watcher.media_processing, "reconcile_all_media", lambda *_a, **_kw: 0
    )
    monkeypatch.setattr(
        file_watcher.freshness, "external_pending_epoch", lambda _root: None
    )
    monkeypatch.setattr(file_watcher.freshness, "external_pending", lambda _root: False)
    monkeypatch.setattr(
        file_watcher.FileWatcher, "_recover_suspended_graph", lambda _self: None
    )
    monkeypatch.setattr(
        deferred_index,
        "inspect_embedding_freshness",
        lambda _root, paths, **_kwargs: {
            rel: deferred_index.EmbeddingFreshness.STALE for rel in paths
        },
    )

    def replay(root: Path, _paths: list[Path], receipts):  # noqa: ANN001
        deferred_index.clear_receipts(root, list(receipts))
        return SimpleNamespace(status="completed")

    monkeypatch.setattr(index_sync, "replay_deferred_embedding", replay)
    watcher = file_watcher.FileWatcher(vault)
    counts = [deferred_index.status(vault)["count"]]
    maximum_passes = math.ceil(counts[0] / cap)
    for _ in range(maximum_passes):
        watcher._reconcile_once(seed=False)
        counts.append(deferred_index.status(vault)["count"])
        if counts[-1] == 0:
            break

    assert counts[-1] == 0
    assert all(
        before > after
        for before, after in zip(counts[:-1], counts[1:], strict=True)
    )
    assert all(
        before - after <= cap
        for before, after in zip(counts[:-1], counts[1:], strict=True)
    )


def test_reconcile_drift_spends_budget_before_deferred_drain(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = mode.WatcherPolicy(0.5, 300.0, 32, 25, False)
    watcher = file_watcher.FileWatcher(vault)
    monkeypatch.setattr(watcher, "_watcher_policy", lambda: policy)
    monkeypatch.setattr(file_watcher.freshness, "SCOPES", ("kb",))
    monkeypatch.setattr(
        file_watcher.freshness,
        "reconcile",
        lambda *_a, **_kw: SimpleNamespace(drifted=True, changed=(), deleted=()),
    )
    monkeypatch.setattr(
        file_watcher.media_processing, "reconcile_all_media", lambda *_a, **_kw: 0
    )
    monkeypatch.setattr(
        file_watcher.freshness, "external_pending_epoch", lambda _root: None
    )
    monkeypatch.setattr(file_watcher.freshness, "external_pending", lambda _root: False)
    monkeypatch.setattr(watcher, "_recover_suspended_graph", lambda: None)
    monkeypatch.setattr(watcher, "_dispatch_reconcile_delta", lambda *_a: 7)
    monkeypatch.setattr("exomem.bm25.warm", lambda *_a: None)
    limits: list[int | None] = []
    monkeypatch.setattr(
        index_sync,
        "drain_deferred_work",
        lambda _root, *, limit=None: limits.append(limit) or 0,
    )

    watcher._reconcile_once(seed=False)

    assert limits == [18]


def test_performance_reconcile_backlog_uses_the_smaller_live_batch_cap(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = mode.WatcherPolicy(0.5, 300.0, 32, 500, False)
    watcher = file_watcher.FileWatcher(vault)
    monkeypatch.setattr(watcher, "_watcher_policy", lambda: policy)
    monkeypatch.setattr(file_watcher.freshness, "SCOPES", ())
    monkeypatch.setattr(
        file_watcher.media_processing, "reconcile_all_media", lambda *_a, **_kw: 0
    )
    monkeypatch.setattr(
        file_watcher.freshness, "external_pending_epoch", lambda _root: None
    )
    monkeypatch.setattr(file_watcher.freshness, "external_pending", lambda _root: False)
    monkeypatch.setattr(watcher, "_recover_suspended_graph", lambda: None)
    limits: list[int | None] = []
    monkeypatch.setattr(
        index_sync,
        "drain_deferred_work",
        lambda _root, *, limit=None: limits.append(limit) or 0,
    )

    watcher._reconcile_once(seed=False)

    assert limits == [32]


def test_bounded_full_drain_limits_incomplete_batch_isolation(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rels = [f"Knowledge Base/full-{index:02d}.md" for index in range(10)]
    for rel in rels:
        target = vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# queued\n", encoding="utf-8")
    deferred_index.add_full(vault, rels)
    monkeypatch.setattr(
        index_sync, "recover_full_receipt_graph_epoch", lambda _root: True
    )
    attempts: list[list[str]] = []

    def incomplete(_root: Path, paths: list[Path]):
        attempts.append([path.relative_to(vault).as_posix() for path in paths])
        return index_sync.IndexSyncReport(
            "upsert",
            tuple(attempts[-1]),
            tuple(attempts[-1]),
            (index_sync.IndexComponentOutcome("lexstore", "degraded", "failed"),),
        )

    monkeypatch.setattr(index_sync, "upsert_after_write", incomplete)

    assert index_sync.drain_deferred_work(vault, limit=10) == 0
    assert attempts[0] == rels
    assert attempts[1:] == [[rel] for rel in rels[:4]]
    assert set(deferred_index.list_full_paths(vault)) == set(rels)


def test_bounded_semantic_drain_limits_incomplete_batch_isolation(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rels = [f"Knowledge Base/semantic-{index:02d}.md" for index in range(10)]
    for rel in rels:
        target = vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# queued\n", encoding="utf-8")
    deferred_index.add(vault, rels)
    monkeypatch.setattr(
        deferred_index,
        "inspect_embedding_freshness",
        lambda _root, paths, **_kwargs: {
            rel: deferred_index.EmbeddingFreshness.STALE for rel in paths
        },
    )
    attempts: list[list[str]] = []

    def incomplete(_root: Path, _paths: list[Path], receipts):  # noqa: ANN001
        attempts.append([receipt.rel_path for receipt in receipts])
        return SimpleNamespace(status="degraded")

    monkeypatch.setattr(index_sync, "replay_deferred_embedding", incomplete)

    assert index_sync.drain_deferred_work(vault, limit=10) == 0
    assert attempts[0] == rels
    assert attempts[1:] == [[rel] for rel in rels[:4]]
    assert set(deferred_index.list_paths(vault)) == set(rels)


def test_single_slot_bounded_drain_alternates_between_full_and_semantic_queues(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    full_rel = "Knowledge Base/full.md"
    semantic_rel = "Knowledge Base/semantic.md"
    for rel in (full_rel, semantic_rel):
        target = vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# queued\n", encoding="utf-8")
    deferred_index.add_full(vault, [full_rel])
    deferred_index.add(vault, [semantic_rel])
    monkeypatch.setattr(
        index_sync, "recover_full_receipt_graph_epoch", lambda _root: True
    )
    full_attempts: list[list[str]] = []

    def incomplete_full(_root: Path, paths: list[Path]):
        rels = [path.relative_to(vault).as_posix() for path in paths]
        full_attempts.append(rels)
        return index_sync.IndexSyncReport(
            "upsert",
            tuple(rels),
            tuple(rels),
            (index_sync.IndexComponentOutcome("lexstore", "degraded", "failed"),),
        )

    semantic_attempts: list[list[str]] = []

    def complete_semantic(root: Path, _paths: list[Path], receipts):  # noqa: ANN001
        semantic_attempts.append([receipt.rel_path for receipt in receipts])
        deferred_index.clear_receipts(root, list(receipts))
        return SimpleNamespace(status="completed")

    monkeypatch.setattr(index_sync, "upsert_after_write", incomplete_full)
    monkeypatch.setattr(
        deferred_index,
        "inspect_embedding_freshness",
        lambda _root, paths, **_kwargs: {
            rel: deferred_index.EmbeddingFreshness.STALE for rel in paths
        },
    )
    monkeypatch.setattr(index_sync, "replay_deferred_embedding", complete_semantic)

    assert index_sync.drain_deferred_work(vault, limit=1) == 0
    assert index_sync.drain_deferred_work(vault, limit=1) == 1

    assert full_attempts == [[full_rel], [full_rel]]
    assert semantic_attempts == [[semantic_rel]]
    assert deferred_index.list_paths(vault) == []


def test_single_slot_startup_drains_both_queues_across_restarts(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    full_rel = "Knowledge Base/startup-full.md"
    semantic_rel = "Knowledge Base/startup-semantic.md"
    for rel in (full_rel, semantic_rel):
        target = vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# queued\n", encoding="utf-8")
    deferred_index.add_full(vault, [full_rel])
    deferred_index.add(vault, [semantic_rel])
    policy = mode.WatcherPolicy(0.5, 300.0, 1, 500, False)
    monkeypatch.setattr(file_watcher.freshness, "SCOPES", ())
    monkeypatch.setattr(
        file_watcher.FileWatcher, "_watcher_policy", lambda _self: policy
    )
    monkeypatch.setattr(
        file_watcher.FileWatcher,
        "_validate_existing_graph_on_seed",
        lambda _self: True,
    )
    monkeypatch.setattr(
        index_sync, "recover_full_receipt_graph_epoch", lambda _root: True
    )

    def incomplete_full(_root: Path, paths: list[Path]):
        rels = tuple(path.relative_to(vault).as_posix() for path in paths)
        return index_sync.IndexSyncReport(
            "upsert",
            rels,
            rels,
            (index_sync.IndexComponentOutcome("lexstore", "degraded", "failed"),),
        )

    semantic_attempts: list[str] = []

    def complete_semantic(root: Path, _paths: list[Path], receipts):  # noqa: ANN001
        semantic_attempts.extend(receipt.rel_path for receipt in receipts)
        deferred_index.clear_receipts(root, list(receipts))
        return SimpleNamespace(status="completed")

    monkeypatch.setattr(index_sync, "upsert_after_write", incomplete_full)
    monkeypatch.setattr(
        deferred_index,
        "inspect_embedding_freshness",
        lambda _root, paths, **_kwargs: {
            rel: deferred_index.EmbeddingFreshness.STALE for rel in paths
        },
    )
    monkeypatch.setattr(index_sync, "replay_deferred_embedding", complete_semantic)

    file_watcher.FileWatcher(vault)._reconcile_once(seed=True)
    file_watcher.FileWatcher(vault)._reconcile_once(seed=True)

    assert semantic_attempts == [semantic_rel]
    assert deferred_index.list_paths(vault) == []


def test_mixed_drain_turn_claim_serializes_concurrent_callers(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deferred_index.add(vault, ["Knowledge Base/queued.md"])
    real_connect = deferred_index._connect
    simultaneous_select = threading.Barrier(2)

    class ConnectionProxy:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def __enter__(self):
            self._connection.__enter__()
            return self

        def __exit__(self, *args):  # noqa: ANN002
            return self._connection.__exit__(*args)

        def execute(self, sql: str, *args, **kwargs):  # noqa: ANN002, ANN003
            if sql.startswith("SELECT value") and not self._connection.in_transaction:
                simultaneous_select.wait(timeout=5)
            return self._connection.execute(sql, *args, **kwargs)

        def __getattr__(self, name: str):
            return getattr(self._connection, name)

    monkeypatch.setattr(
        deferred_index,
        "_connect",
        lambda *args, **kwargs: ConnectionProxy(real_connect(*args, **kwargs)),
    )
    start = threading.Barrier(3)
    outcomes: list[str] = []

    def claim() -> None:
        start.wait(timeout=5)
        outcomes.append(deferred_index.claim_mixed_drain_queue(vault))

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(outcomes) == ["full", "semantic"]


def test_unbounded_full_drain_isolates_every_incomplete_receipt(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rels = [f"Knowledge Base/full-{index:02d}.md" for index in range(10)]
    for rel in rels:
        target = vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# queued\n", encoding="utf-8")
    deferred_index.add_full(vault, rels)
    monkeypatch.setattr(
        index_sync, "recover_full_receipt_graph_epoch", lambda _root: True
    )
    attempts: list[list[str]] = []

    def incomplete(_root: Path, paths: list[Path]):
        rel_paths = [path.relative_to(vault).as_posix() for path in paths]
        attempts.append(rel_paths)
        return index_sync.IndexSyncReport(
            "upsert",
            tuple(rel_paths),
            tuple(rel_paths),
            (index_sync.IndexComponentOutcome("lexstore", "degraded", "failed"),),
        )

    monkeypatch.setattr(index_sync, "upsert_after_write", incomplete)

    assert index_sync.drain_deferred_work(vault) == 0
    assert attempts == [rels, *[[rel] for rel in rels]]


def test_unbounded_semantic_drain_isolates_every_incomplete_receipt(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rels = [f"Knowledge Base/semantic-{index:02d}.md" for index in range(10)]
    for rel in rels:
        target = vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# queued\n", encoding="utf-8")
    deferred_index.add(vault, rels)
    monkeypatch.setattr(
        deferred_index,
        "inspect_embedding_freshness",
        lambda _root, paths, **_kwargs: {
            rel: deferred_index.EmbeddingFreshness.STALE for rel in paths
        },
    )
    attempts: list[list[str]] = []

    def incomplete(_root: Path, _paths: list[Path], receipts):  # noqa: ANN001
        attempts.append([receipt.rel_path for receipt in receipts])
        return SimpleNamespace(status="degraded")

    monkeypatch.setattr(index_sync, "replay_deferred_embedding", incomplete)

    assert index_sync.drain_deferred_work(vault) == 0
    assert attempts == [rels, *[[rel] for rel in rels]]


def test_doctor_warns_on_deferred_queue_fraction(
    vault: Path,
) -> None:
    from exomem import lexstore

    sidecar = lexstore.lexical_path(vault)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(sidecar) as connection:
        connection.execute("CREATE TABLE pages (id INTEGER PRIMARY KEY)")
        connection.executemany(
            "INSERT INTO pages DEFAULT VALUES", [() for _ in range(100)]
        )
    deferred_index.add_full(
        vault, [f"Knowledge Base/deferred/{index}.md" for index in range(11)]
    )

    check = doctor._check_deferred_index_backlog(vault)

    assert check is not None
    assert check.status == "warn"
    assert "full_upserts" in check.message
    assert "11" in check.message


def test_full_queue_status_names_a_runnable_action(vault: Path) -> None:
    deferred_index.add_full(vault, ["Knowledge Base/deferred.md"])
    status = deferred_index.full_status(vault)
    assert status["next_action"] == f'exomem index --vault "{vault}" --scope vault'


@pytest.mark.parametrize("error_type", [PermissionError, OSError])
def test_mode_permission_failure_is_one_actionable_line_and_cleans_tmp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error_type: type[OSError],
) -> None:
    config = tmp_path / "config.json"
    config.write_text('{"schema": 1, "mode": "quiet"}', encoding="utf-8")
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(config))

    def deny(_source: Path, _target: Path) -> None:
        raise error_type("access denied")

    monkeypatch.setattr(mode.os, "replace", deny)

    assert _mode_main(["performance"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1
    assert str(config) in captured.err
    assert "grant Modify" in captured.err
    assert not config.with_suffix(".json.tmp").exists()
    assert mode.read_config()["mode"] == "quiet"


def test_mode_readback_mismatch_fails_with_the_persisted_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config.json"
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(config))

    def persist_different_value(_requested: str) -> Path:
        config.write_text('{"schema": 1, "mode": "normal"}', encoding="utf-8")
        return config

    monkeypatch.setattr(mode, "write_mode", persist_different_value)

    assert _mode_main(["performance"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1
    assert str(config) in captured.err
    assert "read back 'normal' instead of 'performance'" in captured.err
    assert "retry" in captured.err


def test_unsafe_windows_runtime_dacl_error_names_path_and_exact_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "pre-existing-runtime"
    runtime.mkdir()
    sid = "S-1-5-21-1-2-3-4"
    closed: list[int] = []
    monkeypatch.setattr(
        mutation_lock, "_windows_open_path", lambda _path, *, directory: 41
    )
    monkeypatch.setattr(mutation_lock, "_windows_close_handle", closed.append)
    monkeypatch.setattr(
        mutation_lock,
        "_windows_dacl_sddl",
        lambda _path: "D:(A;OICI;FA;;;WD)",
    )

    with pytest.raises(RuntimeError) as raised:
        mutation_lock._validate_windows_runtime_entry(
            runtime, directory=True, sid=sid
        )

    message = str(raised.value)
    assert "\n" not in message
    assert str(runtime) in message
    assert "icacls" in message
    assert "/inheritance:r" in message
    assert f"*{sid}:(OI)(CI)F" in message
    assert "*S-1-5-18" in message
    assert "*S-1-5-32-544" in message
    assert "(F)" not in message
    assert " /T" not in message
    assert closed == [41]


def test_windows_runtime_file_remedy_uses_noninheriting_full_control(
    tmp_path: Path,
) -> None:
    target = tmp_path / "runtime's state.sqlite"
    command = mutation_lock._windows_private_dacl_repair_command(
        target, "S-1-5-18", directory=False
    )

    assert str(target).replace("'", "''") in command
    assert "*S-1-5-18:F" in command
    assert "*S-1-5-32-544:F" in command
    assert "(OI)" not in command
    assert command.count("*S-1-5-18") == 1


def test_preexisting_unsafe_runtime_fails_before_idempotency_sqlite_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import writer_lease

    state_dir = tmp_path / "pre-existing-state"
    state_dir.mkdir()
    sid = "S-1-5-21-1-2-3-4"
    monkeypatch.setattr(
        mutation_lock, "_windows_open_path", lambda _path, *, directory: 52
    )
    monkeypatch.setattr(mutation_lock, "_windows_close_handle", lambda _handle: None)
    monkeypatch.setattr(
        mutation_lock,
        "_windows_dacl_sddl",
        lambda _path: "D:(A;OICI;FA;;;WD)",
    )
    monkeypatch.setattr(
        mutation_lock,
        "prepare_windows_idempotency_runtime_paths",
        lambda state, _owners: mutation_lock._validate_windows_runtime_entry(
            state, directory=True, sid=sid
        ),
    )
    monkeypatch.setattr(writer_lease, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        writer_lease.sqlite3,
        "connect",
        lambda *_a, **_kw: pytest.fail("unsafe runtime reached sqlite"),
    )

    with pytest.raises(mutation_lock.WindowsRuntimeDaclError) as raised:
        writer_lease.IdempotencyStore(
            state_dir / "idempotency.sqlite", secret_protector=object()
        )

    assert str(state_dir) in str(raised.value)
    assert "icacls.exe" in str(raised.value)


def test_doctor_fails_with_the_actionable_runtime_dacl_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = Path("C:" + "\\") / "ProgramData" / "exomem" / "writer-lease"
    remediation = f"icacls.exe '{path}' /inheritance:r"
    error = mutation_lock.WindowsRuntimeDaclError(path, remediation)

    class BrokenStore:
        def validate_runtime_state(self):
            raise error

        def status_summary(self):
            pytest.fail("doctor summarized an unsafe runtime")

    monkeypatch.setattr(
        "exomem.writer_lease.get_manager",
        lambda: SimpleNamespace(idempotency=BrokenStore()),
    )

    check = doctor._check_idempotency_store()

    assert check.status == "fail"
    assert str(path) in check.message
    assert check.remediation == remediation
