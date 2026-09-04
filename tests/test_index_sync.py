from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import (
    deferred_index,
    embeddings,
    epistemic_graph,
    freshness,
    graph_sync,
    index_sync,
    readiness,
    semantic_contract,
)
from exomem import find as find_module
from exomem import vault as vault_module


def _outcome(report: index_sync.IndexSyncReport, component: str):
    return next(item for item in report.components if item.component == component)


def test_embedding_upsert_status_distinguishes_disabled_and_warmup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "Knowledge Base" / "Notes" / "item.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Item\n", encoding="utf-8")

    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    disabled = embeddings.upsert_after_write_status(tmp_path, [target])
    assert disabled.status == "disabled"
    assert disabled.code == "embeddings_disabled"
    assert embeddings.upsert_after_write(tmp_path, [target]) is False

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS")
    monkeypatch.setattr(embeddings, "_IMPORT_FAILED", False)
    readiness.reset()
    readiness.begin_warm()
    deferred_index.clear(tmp_path)
    try:
        warmup = embeddings.upsert_after_write_status(tmp_path, [target])
        assert warmup.status == "deferred"
        assert warmup.code == "deferred_warmup"
        assert readiness.snapshot()["deferred_counts"]["embeddings"] == 1
        assert deferred_index.status(tmp_path)["count"] == 1
    finally:
        readiness.reset()
        deferred_index.clear(tmp_path)


def test_embedding_upsert_status_distinguishes_completed_and_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "Knowledge Base" / "Notes" / "item.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Item\n", encoding="utf-8")
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "_IMPORT_FAILED", False)
    readiness.reset()
    monkeypatch.setattr(embeddings, "get_model", lambda: object())
    page = SimpleNamespace(rel_path="Knowledge Base/Notes/item.md")
    monkeypatch.setattr(find_module._CACHE, "get", lambda *_args: page)
    monkeypatch.setattr(embeddings, "_chunks_for_page", lambda *_args: ["item"])
    monkeypatch.setattr(embeddings, "_embed_live_chunks", lambda chunks: [[1.0]])

    class _Index:
        def upsert_file(self, *_args) -> None:
            return None

        def delete_file(self, *_args) -> None:
            return None

        def delete_semantic_units(self, *_args) -> None:
            return None

    monkeypatch.setattr(embeddings, "get_embedding_index", lambda _root: _Index())

    completed = embeddings.upsert_after_write_status(tmp_path, [target])
    assert completed.status == "completed"
    assert completed.code == "embedding_upsert_completed"

    monkeypatch.setattr(
        embeddings,
        "_embed_live_chunks",
        lambda _chunks: (_ for _ in ()).throw(RuntimeError("private backend detail")),
    )
    degraded = embeddings.upsert_after_write_status(tmp_path, [target])
    assert degraded.status == "degraded"
    assert degraded.code == "embedding_encode_failed"
    assert "private backend detail" not in repr(degraded)
    assert embeddings.upsert_after_write(tmp_path, [target]) is False

    monkeypatch.setattr(
        embeddings,
        "get_model",
        lambda: (_ for _ in ()).throw(RuntimeError("private model detail")),
    )
    assert embeddings.upsert_after_write(tmp_path, [target]) is False


def test_embedding_legacy_bool_ignores_claim_auxiliary_only_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import claims

    target = tmp_path / "Knowledge Base" / "Notes" / "item.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Item\n", encoding="utf-8")
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "_IMPORT_FAILED", False)
    readiness.reset()
    monkeypatch.setattr(embeddings, "get_model", lambda: object())
    page = SimpleNamespace(rel_path="Knowledge Base/Notes/item.md")
    monkeypatch.setattr(find_module._CACHE, "get", lambda *_args: page)
    monkeypatch.setattr(embeddings, "_chunks_for_page", lambda *_args: ["item"])
    monkeypatch.setattr(embeddings, "_embed_live_chunks", lambda chunks: [[1.0]])

    class _Index:
        def upsert_file(self, *_args) -> None:
            return None

        def delete_file(self, *_args) -> None:
            return None

        def delete_semantic_units(self, *_args) -> None:
            return None

    monkeypatch.setattr(embeddings, "get_embedding_index", lambda _root: _Index())
    monkeypatch.setattr(claims, "claim_level_enabled", lambda: True)
    monkeypatch.setattr(
        claims,
        "upsert_claims_after_write",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("private claim detail")),
    )

    status = embeddings.upsert_after_write_status(tmp_path, [target])

    assert status.status == "degraded"
    assert status.code == "embedding_auxiliary_failed"
    assert "private claim detail" not in repr(status)
    assert embeddings.upsert_after_write(tmp_path, [target]) is True


def test_embedding_delete_status_distinguishes_disabled_completed_and_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rel = "Knowledge Base/Notes/item.md"
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    assert embeddings.delete_after_remove_status(tmp_path, [rel]).status == "disabled"

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS")
    monkeypatch.setattr(embeddings, "_IMPORT_FAILED", False)

    class _Index:
        def __init__(self) -> None:
            self.fail = False

        def delete_file(self, _rel: str) -> None:
            if self.fail:
                raise RuntimeError("private delete detail")

    index = _Index()
    monkeypatch.setattr(embeddings, "get_embedding_index", lambda _root: index)
    completed = embeddings.delete_after_remove_status(tmp_path, [rel])
    assert completed.status == "completed"
    assert completed.code == "embedding_delete_completed"

    index.fail = True
    degraded = embeddings.delete_after_remove_status(tmp_path, [rel])
    assert degraded.status == "degraded"
    assert degraded.code == "embedding_delete_failed"
    assert "private delete detail" not in repr(degraded)


def test_upsert_report_contains_failures_and_continues_single_fanout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import epistemic_graph, find, lexstore, memory_refs

    target = tmp_path / "Knowledge Base" / "Notes" / "item.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Item\n", encoding="utf-8")
    calls: list[str] = []

    def _legacy_failure(_root, _paths) -> None:
        calls.append("lexstore")
        raise RuntimeError("private lexstore detail")

    monkeypatch.setattr(lexstore, "upsert_after_write", _legacy_failure)
    monkeypatch.setattr(
        memory_refs,
        "upsert_after_write",
        lambda _root, _paths: calls.append("memory_refs"),
    )
    monkeypatch.setattr(
        epistemic_graph,
        "upsert_after_write",
        lambda _root, _paths: calls.append("epistemic_graph"),
    )
    monkeypatch.setattr(
        find,
        "on_resolver_files_changed",
        lambda *_args: calls.append("resolver"),
    )
    monkeypatch.setattr(
        embeddings,
        "upsert_after_write_status",
        lambda _root, _paths: embeddings.EmbeddingSyncStatus(
            status="completed", code="embedding_upsert_completed", eligible_count=1
        ),
    )

    report = index_sync.upsert_after_write(tmp_path, [target])

    # Identity maintenance runs before semantic leaves so a simultaneously
    # suppressed Record can be purged before any semantic insertion.
    assert calls == ["memory_refs", "resolver", "lexstore", "epistemic_graph"]
    assert report.requested_paths == ("Knowledge Base/Notes/item.md",)
    assert report.eligible_paths == report.requested_paths
    assert _outcome(report, "lexstore").outcome == "degraded"
    assert _outcome(report, "lexstore").code == "dispatch_failed"
    assert _outcome(report, "memory_refs").code == "accepted_unverified"
    assert _outcome(report, "resolver").outcome == "completed"
    assert _outcome(report, "epistemic_graph").outcome == "failed"
    assert _outcome(report, "epistemic_graph").code == "graph_outcome_missing"
    assert _outcome(report, "embeddings").outcome == "completed"
    assert report.reconcile_required is True
    assert "private lexstore detail" not in repr(report)


def test_upsert_report_marks_synchronous_legacy_callbacks_completed(
    tmp_path: Path,
) -> None:
    target = tmp_path / "Knowledge Base" / "Notes" / "item.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Item\n", encoding="utf-8")

    report = index_sync.upsert_after_write(tmp_path, [target])

    assert _outcome(report, "lexstore").outcome == "completed"
    assert _outcome(report, "memory_refs").outcome == "completed"
    assert all(item.code != "accepted_unverified" for item in report.components)


def test_watcher_upsert_routes_full_vault_generation_only_to_lexstore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import lexstore

    target = tmp_path / "Knowledge Base" / "Notes" / "item.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Item\n", encoding="utf-8")
    outside = tmp_path / "Sources" / "item.md"
    outside.parent.mkdir(parents=True)
    outside.write_text("# Outside item\n", encoding="utf-8")
    removed_rel = "Knowledge Base/Notes/removed.md"
    outside_removed_rel = "Sources/removed.md"
    calls: list[tuple[list[Path], list[str]]] = []
    embedding_calls: list[list[Path]] = []
    monkeypatch.setattr(
        lexstore,
        "apply_watcher_batch",
        lambda _root, paths, rels: calls.append((list(paths), list(rels))) or True,
    )
    monkeypatch.setattr(
        embeddings,
        "upsert_after_write_status",
        lambda _root, paths: (
            embedding_calls.append(list(paths))
            or embeddings.EmbeddingSyncStatus(
                "completed",
                "embedding_upsert_completed",
                len(paths),
            )
        ),
    )

    report = index_sync.upsert_after_write(
        tmp_path,
        [target, outside],
        publish_corpus_change=False,
        watcher_deleted_rel_paths=[removed_rel, outside_removed_rel],
    )

    assert calls == [([target, outside], [removed_rel, outside_removed_rel])]
    assert embedding_calls == [[target]]
    assert _outcome(report, "lexstore").outcome == "completed"


def test_watcher_outside_delete_only_wakes_lexstore_not_kb_consumers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import lexstore

    calls: list[tuple[list[Path], list[str]]] = []
    monkeypatch.setattr(
        lexstore,
        "apply_watcher_batch",
        lambda _root, paths, rels: calls.append((list(paths), list(rels))) or True,
    )
    monkeypatch.setattr(
        embeddings,
        "upsert_after_write_status",
        lambda *_args, **_kwargs: pytest.fail("delete-only vault batch woke embeddings"),
    )
    monkeypatch.setattr(
        epistemic_graph,
        "upsert_after_write",
        lambda *_args, **_kwargs: pytest.fail("delete-only vault batch woke graph"),
    )

    report = index_sync.upsert_after_write(
        tmp_path,
        [],
        publish_corpus_change=False,
        watcher_deleted_rel_paths=["Sources/removed.md"],
    )

    assert calls == [([], ["Sources/removed.md"])]
    assert _outcome(report, "lexstore").outcome == "completed"
    assert _outcome(report, "embeddings").code == "no_eligible_paths"
    assert _outcome(report, "epistemic_graph").code == "no_graph_input"


def test_delete_report_marks_known_synchronous_callbacks_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import epistemic_graph

    rel = "Knowledge Base/Notes/item.md"
    monkeypatch.setattr(index_sync, "publish_corpus_delta", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        epistemic_graph,
        "upsert_after_write",
        lambda *_args, **_kwargs: epistemic_graph.GraphDispatchResult(
            "completed", "incremental_completed"
        ),
    )

    report = index_sync.delete_after_remove(tmp_path, [rel])

    assert _outcome(report, "lexstore").outcome == "completed"
    assert _outcome(report, "memory_refs").outcome == "completed"
    assert _outcome(report, "claims").outcome == "completed"
    assert _outcome(report, "clip").as_dict() == {
        "component": "clip",
        "outcome": "accepted",
        "code": "clip_disabled",
    }
    assert all(item.code != "accepted_unverified" for item in report.components)


def test_watcher_delete_skips_lexstore_after_combined_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem import lexstore

    monkeypatch.setattr(
        lexstore,
        "delete_after_remove",
        lambda *_args: (_ for _ in ()).throw(AssertionError("lexstore dispatched twice")),
    )

    report = index_sync.delete_after_remove(
        tmp_path,
        ["Knowledge Base/Notes/removed.md"],
        publish_corpus_change=False,
        dispatch_lexstore=False,
    )

    assert _outcome(report, "lexstore").as_dict() == {
        "component": "lexstore",
        "outcome": "not_required",
        "code": "watcher_batch_completed",
    }


def test_legacy_callback_internal_failures_report_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import lexstore, memory_refs

    target = tmp_path / "Knowledge Base" / "Notes" / "item.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Item\n", encoding="utf-8")
    lexstore.lexical_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    lexstore.lexical_path(tmp_path).touch()
    monkeypatch.setattr(
        lexstore,
        "get_store",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("lexstore failure")),
    )
    monkeypatch.setattr(
        memory_refs,
        "ReferenceIndex",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("memory ref failure")),
    )

    assert lexstore.upsert_after_write(tmp_path, [target]) is False
    assert memory_refs.upsert_after_write(tmp_path, [target]) is False


def test_durable_defer_report_does_not_enter_embedding_warmup_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import epistemic_graph, find, lexstore, memory_refs

    target = tmp_path / "Knowledge Base" / "Notes" / "item.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Item\n", encoding="utf-8")
    for module in (lexstore, memory_refs):
        monkeypatch.setattr(module, "upsert_after_write", lambda *_args: None)
    monkeypatch.setattr(
        epistemic_graph,
        "upsert_after_write",
        lambda *_args: epistemic_graph.GraphDispatchResult("completed", "incremental_completed"),
    )
    monkeypatch.setattr(find, "on_resolver_files_changed", lambda *_args: None)
    monkeypatch.setattr(
        embeddings,
        "upsert_after_write_status",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not dispatch")),
    )
    readiness.reset()
    readiness.begin_warm()
    deferred_index.clear(tmp_path)
    try:
        report = index_sync.upsert_after_write(tmp_path, [target], defer_semantic=True)
        outcome = _outcome(report, "embeddings")
        assert outcome.outcome == "deferred"
        assert outcome.code == "deferred_durable"
        assert deferred_index.status(tmp_path)["paths"] == ["Knowledge Base/Notes/item.md"]
        assert readiness.snapshot()["deferred_counts"]["embeddings"] == 0
        assert report.reconcile_required is False
    finally:
        readiness.reset()
        deferred_index.clear(tmp_path)


def test_public_warm_defer_replay_does_not_strand_newer_duplicate_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import epistemic_graph, find, lexstore, memory_refs, warmup

    target = tmp_path / "Knowledge Base" / "Notes" / "warm-race.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Warm race\n", encoding="utf-8")
    for module in (lexstore, memory_refs, epistemic_graph):
        monkeypatch.setattr(module, "upsert_after_write", lambda *_args: None)
    monkeypatch.setattr(find, "on_resolver_files_changed", lambda *_args: None)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv("EXOMEM_MODE", "normal")
    monkeypatch.setattr(embeddings, "_IMPORT_FAILED", False)
    monkeypatch.setattr(warmup, "warm_caches", lambda *_args, **_kwargs: {})
    readiness.reset()
    readiness.begin_warm()
    deferred_index.clear(tmp_path)
    try:
        report = index_sync.upsert_after_write(tmp_path, [target])
        assert _outcome(report, "embeddings").code == "deferred_warmup"

        monkeypatch.setattr(
            embeddings,
            "upsert_after_write_status",
            lambda *_args, **_kwargs: embeddings.EmbeddingSyncStatus(
                "completed", "embedding_upsert_completed", 1
            ),
        )
        warmup.warm_all(tmp_path)

        assert deferred_index.snapshot(tmp_path) == []
    finally:
        readiness.reset()
        deferred_index.clear(tmp_path)


def test_durable_defer_with_no_semantic_paths_reports_accepted_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import epistemic_graph, find, lexstore, memory_refs

    target = tmp_path / "Knowledge Base" / "config.json"
    target.parent.mkdir(parents=True)
    target.write_text("{}\n", encoding="utf-8")
    for module in (lexstore, memory_refs, epistemic_graph):
        monkeypatch.setattr(module, "upsert_after_write", lambda *_args: None)
    monkeypatch.setattr(find, "on_resolver_files_changed", lambda *_args: None)
    monkeypatch.setattr(
        embeddings,
        "upsert_after_write_status",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not dispatch")),
    )
    deferred_index.clear(tmp_path)
    try:
        report = index_sync.upsert_after_write(tmp_path, [target], defer_semantic=True)

        outcome = _outcome(report, "embeddings")
        assert outcome.outcome == "accepted"
        assert outcome.code == "no_eligible_paths"
        assert deferred_index.status(tmp_path)["count"] == 0
        assert report.reconcile_required is False
    finally:
        deferred_index.clear(tmp_path)


def test_batch_atomic_write_collector_observes_existing_fanout_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "Knowledge Base" / "Notes" / "item.md"
    report = index_sync.IndexSyncReport(
        operation="upsert",
        requested_paths=("Knowledge Base/Notes/item.md",),
        eligible_paths=("Knowledge Base/Notes/item.md",),
        components=(),
    )
    calls: list[list[Path]] = []

    kwargs_seen: list[dict] = []

    def _upsert(_root: Path, paths: list[Path], **kwargs):
        calls.append(list(paths))
        kwargs_seen.append(kwargs)
        return report

    monkeypatch.setattr(index_sync, "upsert_after_write", _upsert)
    monkeypatch.setattr("exomem.file_watcher.register_self_write", lambda *_args: None)
    collected: list[index_sync.IndexSyncReport] = []

    replaced = vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(target, "# Item\n")],
        vault_root=tmp_path,
        index_reports=collected,
    )

    # Epoch publication wraps the canonical write, but it is internal to the
    # batch: callers only receive the paths they supplied.
    assert replaced == [target]
    floor = graph_sync.read_floor(tmp_path)
    checkpoint = graph_sync.read_checkpoint(tmp_path)
    assert floor is not None
    assert checkpoint is not None
    assert checkpoint.generation == floor.generation
    assert checkpoint.paths == (
        ("Knowledge Base/Notes/item.md", vault_module.content_hash("# Item\n")),
    )
    assert calls == [[target]]
    assert kwargs_seen == [
        {
            "created_paths": [target],
            "publish_corpus_change": False,
        }
    ]
    assert collected == [report]


def test_batch_atomic_write_hides_internal_epoch_paths_without_fanout(tmp_path: Path) -> None:
    target = tmp_path / "Knowledge Base" / "Notes" / "item.md"

    replaced = vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(target, "# Item\n")],
        vault_root=tmp_path,
        post_commit_fanout=False,
    )

    assert replaced == [target]
    assert graph_sync.read_floor(tmp_path) is not None
    assert graph_sync.read_checkpoint(tmp_path) is not None


def test_graph_lock_error_reaches_index_sync_as_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import epistemic_graph, find, lexstore, memory_refs
    from exomem.cli_ops import OpError

    target = tmp_path / "Knowledge Base" / "Notes" / "item.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Item\n", encoding="utf-8")
    monkeypatch.setattr(lexstore, "upsert_after_write", lambda *_args: None)
    monkeypatch.setattr(memory_refs, "upsert_after_write", lambda *_args: None)
    monkeypatch.setattr(find, "on_resolver_files_changed", lambda *_args: None)
    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex,
        "refresh_paths",
        lambda *_args: (_ for _ in ()).throw(OpError("MUTATION_BUSY", "graph mutation is busy")),
    )
    monkeypatch.setattr(
        embeddings,
        "upsert_after_write_status",
        lambda *_args: embeddings.EmbeddingSyncStatus("completed", "embedding_upsert_completed", 1),
    )

    report = index_sync.upsert_after_write(tmp_path, [target])

    graph = _outcome(report, "epistemic_graph")
    assert graph.outcome == "failed"
    assert graph.code == "graph_dispatch_failed"
    assert report.reconcile_required is True


def test_publication_failure_withdraws_stale_graph_and_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = tmp_path / "Knowledge Base" / "Notes" / "a.md"
    b = tmp_path / "Knowledge Base" / "Notes" / "b.md"
    a.parent.mkdir(parents=True)
    a.write_text("# A\n\nLinks to [[Old B]].\n", encoding="utf-8")
    b.write_text("---\ntitle: Old B\n---\n# B\n", encoding="utf-8")
    freshness.rebaseline(tmp_path)
    graph = epistemic_graph.EpistemicGraphIndex(tmp_path)
    graph.rebuild_all()
    find_module._get_query_resolver(tmp_path)

    a.write_text("# A\n\nLinks to [[New B]].\n", encoding="utf-8")
    b.write_text("---\ntitle: New B\n---\n# B\n", encoding="utf-8")
    # The WHOLE publish seam fails, so neither half can be blamed. An
    # unattributable failure is deliberately read as registry loss (the
    # conservative classification), which is what keeps the full withdraw --
    # and these assertions -- in force here. A failure attributable to the
    # corpus patch alone is a different, narrower response; see
    # tests/test_freshness_liveness_contract.py.
    monkeypatch.setattr(
        semantic_contract,
        "publish_corpus_files_changed_classified",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("publish failed")),
    )

    report = index_sync.upsert_after_write(tmp_path, [a, b])

    assert report.reconcile_required is True
    assert _outcome(report, "epistemic_graph").code == "publication_failed"
    assert freshness.recall_is_live(tmp_path, "vault") is False
    resolver = find_module.writer_resolver_snapshot(tmp_path)
    resolved, warning = vault_module.normalize_wikilink(
        "New B", tmp_path, resolver=resolver, strict=False
    )
    assert warning is None
    assert resolved == "Knowledge Base/Notes/b"
    assert graph.available() is False


def test_delete_publication_failure_cannot_leave_graph_current_at_old_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = tmp_path / "Knowledge Base" / "Notes" / "a.md"
    b = tmp_path / "Knowledge Base" / "Notes" / "b.md"
    a.parent.mkdir(parents=True)
    a.write_text("# A\n\nLinks to [[B]].\n", encoding="utf-8")
    b.write_text("# B\n", encoding="utf-8")
    freshness.rebaseline(tmp_path)
    graph = epistemic_graph.EpistemicGraphIndex(tmp_path)
    graph.rebuild_all()
    b.unlink()
    # Unattributable (the whole seam fails), so registry loss; see the sibling
    # upsert test above.
    monkeypatch.setattr(
        semantic_contract,
        "publish_corpus_files_changed_classified",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("publish failed")),
    )

    report = index_sync.delete_after_remove(tmp_path, ["Knowledge Base/Notes/b.md"])

    assert report.reconcile_required is True
    assert _outcome(report, "epistemic_graph").code == "publication_failed"
    assert freshness.recall_is_live(tmp_path, "vault") is False
    assert graph.available() is False


def test_delete_report_continues_after_observable_component_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import epistemic_graph, find, lexstore, memory_refs

    calls: list[str] = []
    monkeypatch.setattr(
        lexstore,
        "delete_after_remove",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("private delete detail")),
    )
    monkeypatch.setattr(
        memory_refs,
        "delete_after_remove",
        lambda *_args: calls.append("memory_refs"),
    )
    monkeypatch.setattr(
        epistemic_graph,
        "upsert_after_write",
        lambda *_args: calls.append("epistemic_graph"),
    )
    monkeypatch.setattr(
        embeddings,
        "delete_after_remove_status",
        lambda *_args: embeddings.EmbeddingSyncStatus(
            status="disabled", code="embeddings_disabled", eligible_count=1
        ),
    )
    monkeypatch.setattr(
        find,
        "on_resolver_files_changed",
        lambda *_args: calls.append("resolver"),
    )

    report = index_sync.delete_after_remove(tmp_path, ["Knowledge Base/Notes/item.md"])

    assert calls == ["memory_refs", "epistemic_graph", "resolver"]
    assert _outcome(report, "lexstore").outcome == "degraded"
    assert _outcome(report, "embeddings").outcome == "accepted"
    assert _outcome(report, "embeddings").code == "embeddings_disabled"
    assert report.reconcile_required is True


# ---------------------------------------------------------------------------
# bound-contended-write-index-refresh: warm-up deferral accounting.
#
# A batch write during an embedding warm-up window defers with durable,
# path-exact semantic receipts already recorded.  Declaring that deferral a
# batch failure is what escalated an O(1) cause into whole-vault work: the
# minted full-component receipt re-runs the entire fan-out and a graph epoch
# recovery build on every drain pass.
# ---------------------------------------------------------------------------


def _warm_deferred_note(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real KB note plus a genuinely warming embedding component."""
    target = tmp_path / "Knowledge Base" / "Notes" / "warm-accounting.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        "# Warm accounting\n\nwarm deferral accounting probe\n", encoding="utf-8"
    )
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv("EXOMEM_MODE", "normal")
    monkeypatch.setattr(embeddings, "_IMPORT_FAILED", False)
    readiness.reset()
    readiness.begin_warm()
    deferred_index.clear(tmp_path)
    deferred_index.clear_full(tmp_path)
    return target


def _clean_warm_deferred_note(tmp_path: Path) -> None:
    readiness.reset()
    deferred_index.clear(tmp_path)
    deferred_index.clear_full(tmp_path)


def test_warm_covered_deferral_is_batch_success_and_mints_no_full_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 1.1: a warm-up deferral that durably covered the batch is success.

    Red-first pin of the measured 2026-08-26 escalation: today the
    `deferred_warmup` outcome fails `full_upsert_succeeded` and mints a durable
    full-component receipt for paths whose semantic replay is already durably
    queued path-by-path.
    """
    target = _warm_deferred_note(tmp_path, monkeypatch)
    rel = "Knowledge Base/Notes/warm-accounting.md"
    try:
        report = index_sync.upsert_after_write(tmp_path, [target])
        outcome = _outcome(report, "embeddings")
        assert outcome.outcome == "deferred"
        assert outcome.code == "deferred_warmup"
        # The deferral durably covered the batch before the report is judged.
        assert deferred_index.status(tmp_path)["paths"] == [rel]
        # The covered deferral is batch-report success, not an escalation.
        assert index_sync.full_upsert_succeeded(tmp_path, [target], report) is True
        # A batch with no nameable rel paths can never be blessed by coverage:
        # the empty set is a subset of every queue, so without the guard the
        # acceptance would be vacuous.
        assert index_sync.full_upsert_succeeded(tmp_path, [], report) is False

        completed = vault_module.post_commit_batch_fanout(tmp_path, [target], None, None)
        assert completed is True
        # No durable full-component refresh receipt is minted for the batch;
        # the already-queued semantic replay stays the sole durable demand.
        assert deferred_index.snapshot_full(tmp_path) == []
        assert deferred_index.status(tmp_path)["paths"] == [rel]
    finally:
        _clean_warm_deferred_note(tmp_path)


def test_warm_deferral_accounting_telemetry_counts_both_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 2.2: stable content-free counters behind the module's reset seam."""
    target = _warm_deferred_note(tmp_path, monkeypatch)
    try:
        index_sync.reset_deferral_telemetry()
        report = index_sync.upsert_after_write(tmp_path, [target])
        assert index_sync.full_upsert_succeeded(tmp_path, [target], report) is True
        assert index_sync.deferral_telemetry() == {
            "covered_deferral_accepted": 1,
            "uncovered_deferral_escalated": 0,
        }
        # Judging the same deferred batch against an emptied queue is the
        # uncovered case: fail closed, and count the escalation.
        deferred_index.clear(tmp_path)
        assert index_sync.full_upsert_succeeded(tmp_path, [target], report) is False
        assert index_sync.deferral_telemetry() == {
            "covered_deferral_accepted": 1,
            "uncovered_deferral_escalated": 1,
        }
        index_sync.reset_deferral_telemetry()
        assert all(
            value == 0 for value in index_sync.deferral_telemetry().values()
        )
    finally:
        _clean_warm_deferred_note(tmp_path)


def test_volatile_warm_deferral_still_fails_closed_and_mints_the_demand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 1.2: a warm-up deferral without durable coverage fails closed.

    The durable store refuses both recording seams, so nothing covers the
    batch: the component itself must name the missing coverage, the report
    must fail, the full-component demand must still be minted, and the
    escalation must be counted.
    """
    target = _warm_deferred_note(tmp_path, monkeypatch)
    rel = "Knowledge Base/Notes/warm-accounting.md"

    def refuse(_vault_root, _rels):
        raise OSError("durable defer store unavailable")

    monkeypatch.setattr(deferred_index, "add_receipts", refuse)
    monkeypatch.setattr(deferred_index, "add", refuse)
    try:
        index_sync.reset_deferral_telemetry()
        report = index_sync.upsert_after_write(tmp_path, [target])
        outcome = _outcome(report, "embeddings")
        assert outcome.outcome == "deferred"
        # The component names the missing durable coverage itself, so a stale
        # queue entry for the same path can never bless this deferral.
        assert outcome.code == "deferred_warmup_volatile"
        assert deferred_index.snapshot(tmp_path) == []
        assert index_sync.full_upsert_succeeded(tmp_path, [target], report) is False
        assert index_sync.deferral_telemetry()["uncovered_deferral_escalated"] == 1

        completed = vault_module.post_commit_batch_fanout(tmp_path, [target], None, None)
        assert completed is False
        assert [
            receipt.rel_path for receipt in deferred_index.snapshot_full(tmp_path)
        ] == [rel]
    finally:
        _clean_warm_deferred_note(tmp_path)


def test_stale_receipt_cannot_bless_a_volatile_warm_deferral(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A receipt from an earlier write is not this deferral's coverage.

    The semantic queue retires receipts by per-revision CAS, so only the
    revision bump THIS deferral performed guarantees a replay of the current
    bytes; a stale entry for the same path proves nothing.  The component
    code is therefore the gate: volatile means uncovered, even when the
    queue happens to hold the rel — the tripwire for a mutant that folds
    `deferred_warmup_volatile` into the accepted code set.
    """
    target = _warm_deferred_note(tmp_path, monkeypatch)
    rel = "Knowledge Base/Notes/warm-accounting.md"
    # A durable receipt from an EARLIER write is already queued for the path.
    [stale] = deferred_index.add_receipts(tmp_path, [rel])

    def refuse(_vault_root, _rels):
        raise OSError("durable defer store unavailable")

    monkeypatch.setattr(deferred_index, "add_receipts", refuse)
    monkeypatch.setattr(deferred_index, "add", refuse)
    try:
        report = index_sync.upsert_after_write(tmp_path, [target])
        outcome = _outcome(report, "embeddings")
        assert outcome.code == "deferred_warmup_volatile"
        # The stale receipt is still there — and must not bless the batch.
        assert deferred_index.snapshot(tmp_path) == [stale]
        assert index_sync.full_upsert_succeeded(tmp_path, [target], report) is False
    finally:
        _clean_warm_deferred_note(tmp_path)


def test_warm_deferral_code_alone_cannot_bless_an_uncovered_batch(
    tmp_path: Path,
) -> None:
    """The carve-out must verify durable coverage, never trust the code string.

    Green before and after the fix; the tripwire for a mutant that accepts
    every `deferred_warmup` outcome without proving queue coverage.
    """
    target = tmp_path / "Knowledge Base" / "Notes" / "uncovered.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Uncovered\n", encoding="utf-8")
    rel = "Knowledge Base/Notes/uncovered.md"
    report = index_sync.IndexSyncReport(
        "upsert",
        (rel,),
        (rel,),
        (
            index_sync.IndexComponentOutcome(
                "memory_refs", "completed", "dispatch_completed"
            ),
            index_sync.IndexComponentOutcome(
                "resolver", "completed", "dispatch_completed"
            ),
            index_sync.IndexComponentOutcome(
                "semantic_purge", "completed", "purge_completed"
            ),
            index_sync.IndexComponentOutcome(
                "lexstore", "completed", "dispatch_completed"
            ),
            index_sync.IndexComponentOutcome(
                "epistemic_graph", "completed", "incremental_completed"
            ),
            index_sync.IndexComponentOutcome(
                "embeddings", "deferred", "deferred_warmup"
            ),
        ),
    )
    assert deferred_index.snapshot(tmp_path) == []
    assert index_sync.full_upsert_succeeded(tmp_path, [target], report) is False


def test_contended_lexical_upsert_stays_swallowed_from_the_batch_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 1.3 baseline debt pin — deliberately green before AND after.

    A lexical batch upsert refused under a held publication barrier never
    reaches the batch report: the module-level wrapper discards the refusal,
    `full_upsert_succeeded` stays True, the refusal records no durable demand,
    and recovery is only the process-local deferred registry.  Making the
    refusal observable is the follow-up debt named in the proposal; this pin
    measures it so it cannot drift silently.
    """
    from exomem import lexstore
    from exomem.vault import vault_creation_lock

    if not lexstore.fts5_available():
        pytest.skip("this SQLite build lacks FTS5/trigram")
    monkeypatch.setenv("EXOMEM_MODE", "normal")
    page = tmp_path / "Knowledge Base" / "Notes" / "swallowed.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ntype: insight\ntitle: swallowed\nupdated: 2026-01-01\n---\n"
        "# swallowed\n\nswallowprobe original\n",
        encoding="utf-8",
    )
    # The first search builds the lexical sidecar whole; without it the module
    # wrapper no-ops before ever reaching the publication barrier.
    assert lexstore.search_bm25(tmp_path, "swallowprobe", k=3, scope="kb")
    page.write_text(
        "---\ntype: insight\ntitle: swallowed\nupdated: 2026-01-02\n---\n"
        "# swallowed\n\nswallowprobe revised\n",
        encoding="utf-8",
    )
    scheduled: list[dict] = []
    monkeypatch.setattr(
        lexstore,
        "_schedule_repair",
        lambda root, **kwargs: scheduled.append({"root": root, **kwargs}),
    )

    with vault_creation_lock(tmp_path, "lexical-catalog-publication", timeout=5):
        report = index_sync.upsert_after_write(tmp_path, [page])

    lexical = _outcome(report, "lexstore")
    assert (lexical.outcome, lexical.code) == ("completed", "dispatch_completed")
    assert index_sync.full_upsert_succeeded(tmp_path, [page], report) is True
    assert deferred_index.snapshot_full(tmp_path) == []
    # The semantic queue holds only the ordinary embeddings-disabled heal
    # receipt every write records; the lexical refusal contributed nothing.
    assert [receipt.rel_path for receipt in deferred_index.snapshot(tmp_path)] == [
        "Knowledge Base/Notes/swallowed.md"
    ]
    # The only recovery for the refused rows is the process-local registry, and
    # what that means is a STATE, not a call count: every recovery request this
    # refusal produces is the targeted retry for this one page, and none of them
    # escalates to a whole-corpus rebuild.
    #
    # Asserted as a set for two reasons. It is invariant to how many components
    # observe the one refusal -- since `accelerate-governed-recall` Lane 3 there
    # are two, the fan-out's lexical component and the exact-custody seam that
    # runs at the end of the same batch, finds the rows still stale (of course it
    # does, the write was refused), attempts them once more and is refused by the
    # same held barrier -- so a later lane adding a third legitimate observer does
    # not have to relitigate this. And it is STRONGER than a count: a full-rebuild
    # escalation is `_schedule_repair(root)` with no `deferred_paths`, which adds
    # `(tmp_path, ())` to this set and fails loudly, where a count would have
    # accepted it or rejected it without saying which failure happened.
    #
    # What this pin is for is unchanged and still asserted above: the refusal
    # reaches no batch report, records no durable demand, and the deferred
    # registry remains the only recovery. The second observer is not a per-write
    # cost either -- on an UNCONTENDED write the fan-out's bounded write commits,
    # custody finds nothing stale and schedules nothing at all, which
    # `tests/test_recall_read_cache_custody.py::test_custody_schedules_no_repair_when_the_batch_write_lands`
    # measures directly. It is also load-bearing: with the fan-out's lexical
    # component degraded, custody's registration is the ONLY repair, which is
    # what the M3b/M3c mutant pair in that lane's RESULT.md measures.
    assert scheduled, "the refusal recorded no recovery at all"
    assert {(e["root"], tuple(e.get("deferred_paths") or ())) for e in scheduled} == {
        (tmp_path, (page,))
    }, "a recovery request named a different vault, different paths, or escalated to a full rebuild"


# --- Graph durable-coverage carve-out (tasks 1.1 / 1.2 / 2.1) ---------------
#
# During `recovery_required` the registered checkpoint never equals the live
# one, so EVERY batch write's graph deferral fails the report and mints a
# durable full-component receipt -- even when the deferral already recorded
# per-path graph receipts covering exactly that batch. That funnel is what
# self-sustains: backlog -> recovery_required -> every write mints -> backlog.


_GRAPH_REL = "Knowledge Base/Notes/graph-accounting.md"


def _graph_deferral_report(code: str = "graph_repair_queued") -> index_sync.IndexSyncReport:
    """A batch report whose graph component deferred, everything else done."""
    return index_sync.IndexSyncReport(
        operation="upsert",
        requested_paths=(_GRAPH_REL,),
        eligible_paths=(_GRAPH_REL,),
        components=(
            index_sync.IndexComponentOutcome("memory_refs", "completed", "ok"),
            index_sync.IndexComponentOutcome("resolver", "completed", "ok"),
            index_sync.IndexComponentOutcome("semantic_purge", "completed", "ok"),
            index_sync.IndexComponentOutcome("lexstore", "completed", "ok"),
            index_sync.IndexComponentOutcome("epistemic_graph", "deferred", code),
            index_sync.IndexComponentOutcome("embeddings", "completed", "ok"),
        ),
    )


def test_covered_graph_deferral_during_recovery_is_batch_success(
    tmp_path: Path,
) -> None:
    """Task 1.1: per-path graph receipts covering the batch ARE the demand."""
    target = tmp_path / _GRAPH_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Graph accounting\n", encoding="utf-8")
    report = _graph_deferral_report()

    # No registered checkpoint equals the live one: this is recovery_required,
    # the state in which every graph deferral currently escalates.
    assert graph_sync.registered_checkpoint(tmp_path) is None

    index_sync.reset_deferral_telemetry()
    # Uncovered first: nothing durably queued, so the report must still fail.
    assert index_sync.full_upsert_succeeded(tmp_path, [target], report) is False
    assert index_sync.deferral_telemetry()["uncovered_deferral_escalated"] == 1

    # Now the deferral durably covers the batch's graph-input paths.
    deferred_index.add_graph(tmp_path, [_GRAPH_REL])
    assert deferred_index.snapshot_graph(tmp_path) != []

    index_sync.reset_deferral_telemetry()
    assert index_sync.full_upsert_succeeded(tmp_path, [target], report) is True
    assert index_sync.deferral_telemetry() == {
        "covered_deferral_accepted": 1,
        "uncovered_deferral_escalated": 0,
    }


def test_uncovered_graph_deferral_still_fails_closed(tmp_path: Path) -> None:
    """Task 1.2: a deferral with no covering receipts still mints the demand."""
    target = tmp_path / _GRAPH_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Graph accounting\n", encoding="utf-8")

    # A queue entry for a DIFFERENT path cannot bless this batch.
    deferred_index.add_graph(tmp_path, ["Knowledge Base/Notes/unrelated.md"])
    report = _graph_deferral_report()

    index_sync.reset_deferral_telemetry()
    assert index_sync.full_upsert_succeeded(tmp_path, [target], report) is False
    assert index_sync.deferral_telemetry()["uncovered_deferral_escalated"] == 1


def test_graph_deferral_without_a_coverage_claiming_code_fails_closed(
    tmp_path: Path,
) -> None:
    """Only a code that means "durably queued" may be blessed by receipts.

    `graph_index_disabled` records nothing, so a stale queue entry naming the
    same path must not launder it into a success.
    """
    target = tmp_path / _GRAPH_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Graph accounting\n", encoding="utf-8")
    deferred_index.add_graph(tmp_path, [_GRAPH_REL])

    report = _graph_deferral_report(code="graph_index_disabled")

    assert index_sync.full_upsert_succeeded(tmp_path, [target], report) is False


def test_empty_graph_batch_cannot_be_blessed_by_coverage(tmp_path: Path) -> None:
    """The empty set is a subset of every queue; acceptance must not be vacuous."""
    deferred_index.add_graph(tmp_path, [_GRAPH_REL])
    report = _graph_deferral_report()

    assert index_sync.full_upsert_succeeded(tmp_path, [], report) is False


def test_non_graph_input_paths_cannot_bless_a_graph_deferral(tmp_path: Path) -> None:
    """Coverage is over GRAPH-INPUT paths, not merely over Markdown.

    A note living inside graph_sync's own receipt directory is not a graph
    input, so a batch made only of such paths has nothing to cover and must
    fail closed rather than be accepted vacuously.
    """
    rel = "Knowledge Base/Notes/.graph-commit-receipts/receipt-note.md"
    assert graph_sync.is_graph_input_path(rel) is False
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Receipt note\n", encoding="utf-8")
    deferred_index.add_graph(tmp_path, [rel])

    report = index_sync.IndexSyncReport(
        operation="upsert",
        requested_paths=(rel,),
        eligible_paths=(rel,),
        components=(
            index_sync.IndexComponentOutcome("memory_refs", "completed", "ok"),
            index_sync.IndexComponentOutcome("resolver", "completed", "ok"),
            index_sync.IndexComponentOutcome("semantic_purge", "completed", "ok"),
            index_sync.IndexComponentOutcome("lexstore", "completed", "ok"),
            index_sync.IndexComponentOutcome(
                "epistemic_graph", "deferred", "graph_repair_queued"
            ),
            index_sync.IndexComponentOutcome("embeddings", "completed", "ok"),
        ),
    )

    assert index_sync.full_upsert_succeeded(tmp_path, [target], report) is False
