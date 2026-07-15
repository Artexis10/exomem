from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import deferred_index, embeddings, index_sync, readiness
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
        assert deferred_index.status(tmp_path)["count"] == 0
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

    assert calls == ["lexstore", "memory_refs", "resolver", "epistemic_graph"]
    assert report.requested_paths == ("Knowledge Base/Notes/item.md",)
    assert report.eligible_paths == report.requested_paths
    assert _outcome(report, "lexstore").outcome == "degraded"
    assert _outcome(report, "lexstore").code == "dispatch_failed"
    assert _outcome(report, "memory_refs").code == "accepted_unverified"
    assert _outcome(report, "resolver").outcome == "completed"
    assert _outcome(report, "epistemic_graph").code == "accepted_unverified"
    assert _outcome(report, "embeddings").outcome == "completed"
    assert report.reconcile_required is True
    assert "private lexstore detail" not in repr(report)


def test_durable_defer_report_does_not_enter_embedding_warmup_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import epistemic_graph, find, lexstore, memory_refs

    target = tmp_path / "Knowledge Base" / "Notes" / "item.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Item\n", encoding="utf-8")
    for module in (lexstore, memory_refs, epistemic_graph):
        monkeypatch.setattr(module, "upsert_after_write", lambda *_args: None)
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
        assert deferred_index.status(tmp_path)["paths"] == [
            "Knowledge Base/Notes/item.md"
        ]
        assert readiness.snapshot()["deferred_counts"]["embeddings"] == 0
        assert report.reconcile_required is False
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
        report = index_sync.upsert_after_write(
            tmp_path, [target], defer_semantic=True
        )

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

    def _upsert(_root: Path, paths: list[Path]):
        calls.append(list(paths))
        return report

    monkeypatch.setattr(index_sync, "upsert_after_write", _upsert)
    monkeypatch.setattr("exomem.file_watcher.register_self_write", lambda *_args: None)
    collected: list[index_sync.IndexSyncReport] = []

    replaced = vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(target, "# Item\n")],
        vault_root=tmp_path,
        index_reports=collected,
    )

    assert replaced == [target]
    assert calls == [[target]]
    assert collected == [report]


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
        "delete_after_remove",
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

    report = index_sync.delete_after_remove(
        tmp_path, ["Knowledge Base/Notes/item.md"]
    )

    assert calls == ["memory_refs", "epistemic_graph", "resolver"]
    assert _outcome(report, "lexstore").outcome == "degraded"
    assert _outcome(report, "embeddings").outcome == "accepted"
    assert _outcome(report, "embeddings").code == "embeddings_disabled"
    assert report.reconcile_required is True
