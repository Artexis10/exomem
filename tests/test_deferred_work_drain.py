from __future__ import annotations

import math
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import deferred_index, doctor, file_watcher, index_sync, mode, mutation_lock
from exomem.__main__ import _index_main, _mode_main


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
        return SimpleNamespace(reconcile_required=False)

    monkeypatch.setattr(index_sync, "upsert_after_write", dispatch)

    assert index_sync.drain_deferred_work(vault) == 0
    [remaining] = deferred_index.snapshot_full(vault)
    assert remaining.rel_path == rel
    assert remaining.revision == 2


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
        return SimpleNamespace(reconcile_required=False)

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
        return index_sync.IndexSyncReport("upsert", (rel,), (rel,), outcomes)

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
        return index_sync.IndexSyncReport("upsert", tuple(rels), tuple(rels), ())

    monkeypatch.setattr(index_sync, "upsert_after_write", dispatch)

    assert index_sync.drain_deferred_work(vault, limit=12) == 0
    assert index_sync.drain_deferred_work(vault, limit=12) == 1
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

    assert index_sync.drain_deferred_work(vault, limit=12) == 0
    assert index_sync.drain_deferred_work(vault, limit=12) == 1
    assert good_rel not in deferred_index.list_paths(vault)


def test_quiet_policy_has_nonzero_bounded_deferred_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "quiet")
    policy = mode.watcher_policy()
    assert policy.max_embed_files_per_batch is not None
    assert 0 < policy.max_embed_files_per_batch <= 100
    assert policy.max_reconcile_embed_files == policy.max_embed_files_per_batch


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
    monkeypatch.setattr(writer_lease.os, "name", "nt")
    monkeypatch.setattr(
        writer_lease.sqlite3,
        "connect",
        lambda *_a, **_kw: pytest.fail("unsafe runtime reached sqlite"),
    )

    with pytest.raises(mutation_lock.WindowsRuntimeDaclError) as raised:
        writer_lease.IdempotencyStore(state_dir / "idempotency.sqlite")

    assert str(state_dir) in str(raised.value)
    assert "icacls.exe" in str(raised.value)


def test_doctor_fails_with_the_actionable_runtime_dacl_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = Path(r"C:\ProgramData\exomem\writer-lease")
    remediation = (
        "icacls.exe 'C:\\ProgramData\\exomem\\writer-lease' /inheritance:r"
    )
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
